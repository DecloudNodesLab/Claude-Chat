import os
import asyncio
import json
import subprocess
import threading
import pty
import select
import struct
import fcntl
import termios
from pathlib import Path
from typing import Dict, Optional

from fastapi import WebSocket


def _try_open_pty():
    """Try to open a PTY. Returns (master_fd, slave_fd) or raises."""
    return pty.openpty()


class ShellSession:
    def __init__(self, session_id: str, workspace_dir: Path):
        self.session_id = session_id
        self.workspace_dir = workspace_dir
        self._master_fd: Optional[int] = None
        self._proc = None
        self._buf: list = []
        self._buf_lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._use_pty = False

    def start(self):
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["HOME"] = "/root"
        env["PWD"] = str(self.workspace_dir)
        env["LANG"] = "en_US.UTF-8"
        for k in ("ANTHROPIC_API_KEY", "BASIC_AUTH_PASSWORD", "APP_SECRET_KEY"):
            env.pop(k, None)

        # Try PTY via ptyprocess
        try:
            import ptyprocess
            self._proc = ptyprocess.PtyProcess.spawn(
                ["/bin/bash", "--login"],
                cwd=str(self.workspace_dir),
                env=env,
                dimensions=(24, 80),
            )
            self._use_pty = True
            self._running = True
            self._thread = threading.Thread(target=self._reader_ptyprocess, daemon=True)
            self._thread.start()
            print("[shell] started via ptyprocess", flush=True)
            return
        except Exception as e:
            print(f"[shell] ptyprocess failed: {e}, trying os.openpty...", flush=True)

        # Try raw PTY via os.openpty
        try:
            master_fd, slave_fd = pty.openpty()
            # test it works
            env2 = env.copy()
            self._proc = subprocess.Popen(
                ["/bin/bash", "--login"],
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                cwd=str(self.workspace_dir),
                env=env2,
                close_fds=True,
                preexec_fn=os.setsid,
            )
            os.close(slave_fd)
            self._master_fd = master_fd
            self._use_pty = False  # using raw fd
            self._running = True
            self._thread = threading.Thread(target=self._reader_fd, daemon=True)
            self._thread.start()
            print("[shell] started via os.openpty", flush=True)
            return
        except Exception as e:
            print(f"[shell] os.openpty failed: {e}, using pipe fallback...", flush=True)

        # Pipe fallback (no TTY — limited but functional)
        env["PS1"] = "\\u@workspace:\\w\\$ "
        self._proc = subprocess.Popen(
            ["bash", "--norc", "--noprofile", "-i"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(self.workspace_dir),
            env=env,
            bufsize=0,
        )
        self._use_pty = False
        self._master_fd = None
        self._running = True
        self._thread = threading.Thread(target=self._reader_pipe, daemon=True)
        self._thread.start()
        print("[shell] started via pipe fallback", flush=True)

    def _reader_ptyprocess(self):
        while self._running:
            try:
                data = self._proc.read(4096)
                if data:
                    with self._buf_lock:
                        self._buf.append(data)
            except EOFError:
                break
            except Exception:
                break
        self._running = False

    def _reader_fd(self):
        while self._running:
            try:
                r, _, _ = select.select([self._master_fd], [], [], 0.05)
                if r:
                    try:
                        chunk = os.read(self._master_fd, 4096)
                    except OSError:
                        break
                    if chunk:
                        with self._buf_lock:
                            self._buf.append(chunk)
            except (ValueError, OSError):
                break
        self._running = False

    def _reader_pipe(self):
        while self._running and self._proc:
            try:
                chunk = self._proc.stdout.read(4096)
                if not chunk:
                    break
                with self._buf_lock:
                    self._buf.append(chunk)
            except (OSError, ValueError):
                break
        self._running = False

    def drain(self) -> bytes:
        with self._buf_lock:
            if not self._buf:
                return b""
            out = b"".join(self._buf)
            self._buf.clear()
            return out

    def write(self, data: bytes):
        if not self._running:
            return
        try:
            if hasattr(self._proc, 'write'):  # ptyprocess
                self._proc.write(data)
            elif self._master_fd is not None:  # raw fd
                os.write(self._master_fd, data)
            elif self._proc and self._proc.stdin:  # pipe
                self._proc.stdin.write(data)
                self._proc.stdin.flush()
        except Exception:
            pass

    def resize(self, rows: int, cols: int):
        try:
            if hasattr(self._proc, 'setwinsize'):
                self._proc.setwinsize(rows, cols)
            elif self._master_fd is not None:
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsize)
        except Exception:
            pass

    def inject_command(self, command: str):
        if not command.endswith("\n"):
            command += "\n"
        self.write(command.encode("utf-8", errors="replace"))

    def stop(self):
        self._running = False
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except Exception:
                pass
            self._master_fd = None
        if self._proc:
            try:
                if hasattr(self._proc, 'terminate'):
                    self._proc.terminate(force=True)
                else:
                    self._proc.terminate()
            except Exception:
                pass
            self._proc = None

    def is_alive(self) -> bool:
        if not self._running or self._proc is None:
            return False
        try:
            if hasattr(self._proc, 'isalive'):
                return self._proc.isalive()
            return self._proc.poll() is None
        except Exception:
            return False


class ShellManager:
    def __init__(self, workspace_dir: Path):
        self.workspace_dir = workspace_dir
        self._sessions: Dict[str, ShellSession] = {}

    def get_or_create_session(self, session_id: str = "default") -> ShellSession:
        s = self._sessions.get(session_id)
        if s is None or not s.is_alive():
            if s:
                s.stop()
            s = ShellSession(session_id, self.workspace_dir)
            s.start()
            self._sessions[session_id] = s
        return s

    async def run_command_in_session(
        self,
        command: str,
        session_id: str = "default",
        timeout: float = 30.0,
    ) -> dict:
        s = self._sessions.get(session_id)
        if s and s.is_alive():
            s.inject_command(f"# [Claude] {command}")

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace_dir),
                env={**os.environ, "TERM": "dumb"},
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return {"stdout": "", "stderr": f"Timed out after {int(timeout)}s", "returncode": -1}
            return {
                "stdout": out.decode("utf-8", errors="replace")[:16000],
                "stderr": err.decode("utf-8", errors="replace")[:4000],
                "returncode": proc.returncode,
            }
        except Exception as e:
            return {"stdout": "", "stderr": str(e), "returncode": -1}

    async def handle_websocket(self, websocket: WebSocket, session_id: str):
        session = self.get_or_create_session(session_id)
        await websocket.send_text(json.dumps({"type": "connected", "session_id": session_id}))

        async def pump():
            nonlocal session
            while True:
                await asyncio.sleep(0.02)
                if not session.is_alive():
                    await asyncio.sleep(1)
                    session = self.get_or_create_session(session_id)
                    continue
                chunk = session.drain()
                if chunk:
                    try:
                        await websocket.send_bytes(chunk)
                    except Exception:
                        return

        pump_task = asyncio.create_task(pump())
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                raw = msg.get("bytes") or (
                    msg.get("text", "").encode() if msg.get("text") else None
                )
                if not raw:
                    continue
                if raw[0:1] == b"{":
                    try:
                        obj = json.loads(raw)
                        if obj.get("type") == "resize":
                            session.resize(int(obj["rows"]), int(obj["cols"]))
                            continue
                    except Exception:
                        pass
                session.write(raw)
        except Exception:
            pass
        finally:
            pump_task.cancel()
            try:
                await pump_task
            except asyncio.CancelledError:
                pass
