import os
import asyncio
import json
import ptyprocess
from pathlib import Path
from typing import Dict, Optional

from fastapi import WebSocket


class ShellSession:
    """PTY shell session. Uses asyncio event loop reader for non-blocking output."""

    def __init__(self, session_id: str, workspace_dir: Path):
        self.session_id = session_id
        self.workspace_dir = workspace_dir
        self._proc: Optional[ptyprocess.PtyProcess] = None
        self._output_queue: Optional[asyncio.Queue] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._reader_added = False

    def start(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._output_queue = asyncio.Queue()

        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["HOME"] = str(self.workspace_dir)
        env["PWD"] = str(self.workspace_dir)
        env["LANG"] = "en_US.UTF-8"
        env["LC_ALL"] = "en_US.UTF-8"
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("BASIC_AUTH_PASSWORD", None)
        env.pop("APP_SECRET_KEY", None)

        self._proc = ptyprocess.PtyProcess.spawn(
            ["/bin/bash", "--login"],
            cwd=str(self.workspace_dir),
            env=env,
            dimensions=(24, 80),
        )

        # Register fd with event loop for non-blocking reads
        self._loop.add_reader(self._proc.fd, self._on_readable)
        self._reader_added = True

    def _on_readable(self):
        """Called by event loop when PTY fd has data."""
        try:
            data = self._proc.read(4096)
            if data and self._output_queue is not None:
                self._output_queue.put_nowait(data)
        except EOFError:
            self._remove_reader()
        except Exception:
            self._remove_reader()

    def _remove_reader(self):
        if self._reader_added and self._loop and self._proc:
            try:
                self._loop.remove_reader(self._proc.fd)
            except Exception:
                pass
            self._reader_added = False

    def write(self, data: bytes):
        if self._proc and self.is_alive():
            try:
                self._proc.write(data)
            except Exception:
                pass

    def resize(self, rows: int, cols: int):
        if self._proc and self.is_alive():
            try:
                self._proc.setwinsize(rows, cols)
            except Exception:
                pass

    def inject_command(self, command: str):
        """Inject a visible command line into the terminal."""
        if not command.endswith("\n"):
            command += "\n"
        self.write(command.encode("utf-8", errors="replace"))

    def stop(self):
        self._remove_reader()
        if self._proc:
            try:
                self._proc.terminate(force=True)
            except Exception:
                pass
            self._proc = None

    def is_alive(self) -> bool:
        if self._proc is None:
            return False
        try:
            return self._proc.isalive()
        except Exception:
            return False

    async def get_output(self, timeout: float = 0.1) -> Optional[bytes]:
        try:
            return await asyncio.wait_for(self._output_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None


class ShellManager:
    def __init__(self, workspace_dir: Path):
        self.workspace_dir = workspace_dir
        self._sessions: Dict[str, ShellSession] = {}

    def get_or_create_session(self, session_id: str, loop: asyncio.AbstractEventLoop) -> ShellSession:
        session = self._sessions.get(session_id)
        if session is None or not session.is_alive():
            if session:
                session.stop()
            session = ShellSession(session_id, self.workspace_dir)
            session.start(loop)
            self._sessions[session_id] = session
        return session

    async def run_command_in_session(
        self,
        command: str,
        session_id: str = "default",
        timeout: float = 30.0,
    ) -> dict:
        """
        Execute a command for Claude's tool use.
        Shows the command in the user's terminal via PTY inject.
        Captures output reliably via subprocess.
        """
        session = self._sessions.get(session_id)
        if session and session.is_alive():
            session.inject_command(f"# [Claude] {command}")

        try:
            # Run without sudo - container is already root
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace_dir),
                env={**os.environ, "TERM": "dumb"},
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return {"stdout": "", "stderr": f"Timed out after {int(timeout)}s", "returncode": -1}

            return {
                "stdout": stdout_b.decode("utf-8", errors="replace")[:16000],
                "stderr": stderr_b.decode("utf-8", errors="replace")[:4000],
                "returncode": proc.returncode,
            }
        except Exception as e:
            return {"stdout": "", "stderr": str(e), "returncode": -1}

    async def handle_websocket(self, websocket: WebSocket, session_id: str):
        loop = asyncio.get_event_loop()
        session = self.get_or_create_session(session_id, loop)

        # Send connected confirmation as text
        await websocket.send_text(json.dumps({"type": "connected", "session_id": session_id}))

        async def pump_output():
            """Forward PTY output bytes to browser."""
            nonlocal session
            while True:
                if not session.is_alive():
                    await asyncio.sleep(1)
                    try:
                        session = self.get_or_create_session(session_id, loop)
                    except Exception:
                        pass
                    continue
                data = await session.get_output(timeout=0.1)
                if data:
                    try:
                        await websocket.send_bytes(data)
                    except Exception:
                        return

        output_task = asyncio.create_task(pump_output())

        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break

                raw_bytes = msg.get("bytes")
                raw_text = msg.get("text")

                if raw_bytes:
                    # Check if it's a JSON control message
                    if raw_bytes[0:1] == b"{":
                        try:
                            obj = json.loads(raw_bytes.decode("utf-8"))
                            if obj.get("type") == "resize":
                                session.resize(int(obj["rows"]), int(obj["cols"]))
                                continue
                        except Exception:
                            pass
                    session.write(raw_bytes)

                elif raw_text:
                    try:
                        obj = json.loads(raw_text)
                        if obj.get("type") == "resize":
                            session.resize(int(obj["rows"]), int(obj["cols"]))
                            continue
                    except Exception:
                        pass
                    session.write(raw_text.encode("utf-8"))

        except Exception:
            pass
        finally:
            output_task.cancel()
            try:
                await output_task
            except asyncio.CancelledError:
                pass
