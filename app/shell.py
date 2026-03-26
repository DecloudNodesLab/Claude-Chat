import os
import asyncio
import json
import select
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from fastapi import WebSocket


class ShellSession:
    def __init__(self, session_id: str, workspace_dir: Path):
        self.session_id = session_id
        self.workspace_dir = workspace_dir
        self._proc = None
        self._fd: Optional[int] = None
        self._buf: list = []
        self._buf_lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        import ptyprocess
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["HOME"] = "/root"
        env["PWD"] = str(self.workspace_dir)
        env["LANG"] = "en_US.UTF-8"
        for k in ("ANTHROPIC_API_KEY", "BASIC_AUTH_PASSWORD", "APP_SECRET_KEY"):
            env.pop(k, None)

        self._proc = ptyprocess.PtyProcess.spawn(
            ["/bin/bash", "--login"],
            cwd=str(self.workspace_dir),
            env=env,
            dimensions=(50, 220),
        )
        self._fd = self._proc.fd
        self._running = True
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        print(f"[shell] started pid={self._proc.pid} fd={self._fd}", flush=True)

    def _reader(self):
        fd = self._fd
        while self._running:
            try:
                r, _, _ = select.select([fd], [], [], 0.05)
                if r:
                    try:
                        chunk = os.read(fd, 4096)
                    except OSError:
                        break
                    if chunk:
                        with self._buf_lock:
                            self._buf.append(chunk)
            except (ValueError, OSError):
                break
        self._running = False

    def drain(self) -> bytes:
        with self._buf_lock:
            if not self._buf:
                return b""
            out = b"".join(self._buf)
            self._buf.clear()
            return out

    def read_all_pending(self, wait_ms: int = 50) -> bytes:
        """Drain buffer after a short wait for new data to arrive."""
        time.sleep(wait_ms / 1000)
        return self.drain()

    def write(self, data: bytes):
        if self._fd is not None and self._running:
            try:
                os.write(self._fd, data)
            except OSError:
                pass

    def resize(self, rows: int, cols: int):
        if self._proc and self._running:
            try:
                self._proc.setwinsize(rows, cols)
            except Exception:
                pass

    def inject_command(self, command: str):
        """Send command to PTY as if typed by user."""""
        if not command.endswith("\n"):
            command += "\n"
        self.write(command.encode("utf-8", errors="replace"))

    def inject_claude_command(self, command: str):
        """Inject Claude's command into PTY with green highlight."""""
        # ESC[32m = green, ESC[1m = bold, ESC[0m = reset
        GREEN  = "\x1b[1;32m"
        RESET  = "\x1b[0m"
        # Print highlighted label on its own line, then run command normally
        banner = f"\r\n{GREEN}> [Claude] {command}{RESET}\r\n"
        self.write(banner.encode("utf-8", errors="replace"))

    def stop(self):
        self._running = False
        if self._proc:
            try:
                self._proc.terminate(force=True)
            except Exception:
                pass
            self._proc = None
        self._fd = None

    def is_alive(self) -> bool:
        if not self._running or self._proc is None:
            return False
        try:
            return self._proc.isalive()
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
        """
        Run command BY INJECTING INTO PTY so user sees full output in terminal.
        Also capture output via subprocess for returning to Claude.
        """
        s = self._sessions.get(session_id)

        # Show command in PTY terminal with green highlight
        if s and s.is_alive():
            s.inject_claude_command(command)

        # Capture output separately for Claude's tool result
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self.workspace_dir),
                env={**os.environ, "TERM": "dumb"},
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return {"stdout": "", "stderr": f"Timed out after {int(timeout)}s", "returncode": -1}

            stdout = out.decode("utf-8", errors="replace")[:16000]
            return {"stdout": stdout, "stderr": "", "returncode": proc.returncode}

        except Exception as e:
            return {"stdout": "", "stderr": str(e), "returncode": -1}

    async def handle_websocket(self, websocket: WebSocket, session_id: str):
        session = self.get_or_create_session(session_id)
        await websocket.send_text(json.dumps({"type": "connected", "session_id": session_id}))
        session.resize(50, 220)

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
