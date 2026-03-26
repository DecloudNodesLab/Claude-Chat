import os
import asyncio
import json
import struct
import fcntl
import termios
import pty
import subprocess
import threading
from pathlib import Path
from typing import Dict, Optional

from fastapi import WebSocket


class ShellSession:
    """A single PTY shell session."""

    def __init__(self, session_id: str, workspace_dir: Path):
        self.session_id = session_id
        self.workspace_dir = workspace_dir
        self.master_fd: Optional[int] = None
        self.slave_fd: Optional[int] = None
        self.pid: Optional[int] = None
        self._websockets: list = []
        self._running = False
        self._output_queue: asyncio.Queue = None
        self._loop: asyncio.AbstractEventLoop = None

    def start(self, loop: asyncio.AbstractEventLoop):
        """Fork a PTY shell process."""
        self._loop = loop
        self._output_queue = asyncio.Queue()

        self.master_fd, self.slave_fd = pty.openpty()

        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["HOME"] = str(self.workspace_dir)
        env["PWD"] = str(self.workspace_dir)
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("BASIC_AUTH_PASSWORD", None)
        env.pop("APP_SECRET_KEY", None)

        self.pid = os.fork()
        if self.pid == 0:
            # Child process
            os.close(self.master_fd)
            os.setsid()
            fcntl.ioctl(self.slave_fd, termios.TIOCSCTTY, 0)
            os.dup2(self.slave_fd, 0)
            os.dup2(self.slave_fd, 1)
            os.dup2(self.slave_fd, 2)
            if self.slave_fd > 2:
                os.close(self.slave_fd)
            os.chdir(str(self.workspace_dir))
            os.execvpe("/bin/bash", ["/bin/bash", "--login"], env)
            os._exit(1)

        # Parent process
        os.close(self.slave_fd)
        self.slave_fd = None
        self._running = True

        # Start reader thread
        t = threading.Thread(target=self._read_loop, daemon=True)
        t.start()

    def _read_loop(self):
        """Read from PTY master and put into queue."""
        import select
        while self._running:
            try:
                r, _, _ = select.select([self.master_fd], [], [], 0.1)
                if r:
                    try:
                        data = os.read(self.master_fd, 4096)
                        if data:
                            asyncio.run_coroutine_threadsafe(
                                self._output_queue.put(data),
                                self._loop,
                            )
                    except OSError:
                        break
            except (ValueError, OSError):
                break
        self._running = False

    def write(self, data: bytes):
        """Write to PTY master (input to shell)."""
        if self.master_fd is not None and self._running:
            try:
                os.write(self.master_fd, data)
            except OSError:
                pass

    def resize(self, rows: int, cols: int):
        """Resize the PTY window."""
        if self.master_fd is not None:
            try:
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
            except OSError:
                pass

    def inject_command(self, command: str):
        """Inject a command into the shell as if typed by user."""
        if command and not command.endswith("\n"):
            command += "\n"
        self.write(command.encode("utf-8"))

    def stop(self):
        """Stop the shell session."""
        self._running = False
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        if self.pid is not None:
            try:
                os.kill(self.pid, 9)
                os.waitpid(self.pid, os.WNOHANG)
            except OSError:
                pass
            self.pid = None

    def is_alive(self) -> bool:
        if not self._running or self.pid is None:
            return False
        try:
            result = os.waitpid(self.pid, os.WNOHANG)
            if result[0] != 0:
                self._running = False
                return False
            return True
        except ChildProcessError:
            self._running = False
            return False

    async def get_output(self, timeout: float = 0.05) -> Optional[bytes]:
        """Get pending output from the shell."""
        try:
            return await asyncio.wait_for(self._output_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None


class ShellManager:
    """Manages multiple shell sessions and their WebSocket connections."""

    def __init__(self, workspace_dir: Path):
        self.workspace_dir = workspace_dir
        self._sessions: Dict[str, ShellSession] = {}
        self._default_session_id = "default"

    def get_or_create_session(self, session_id: str, loop: asyncio.AbstractEventLoop) -> ShellSession:
        if session_id not in self._sessions or not self._sessions[session_id].is_alive():
            session = ShellSession(session_id, self.workspace_dir)
            session.start(loop)
            self._sessions[session_id] = session
        return self._sessions[session_id]

    def get_session(self, session_id: str) -> Optional[ShellSession]:
        return self._sessions.get(session_id)

    async def run_command_in_session(
        self,
        command: str,
        session_id: str = "default",
        timeout: float = 30.0,
    ) -> Dict:
        """
        Run a command in the shell session and capture output.
        The command is injected into the PTY so user can see it.
        We use a subprocess for reliable output capture but also inject to PTY for visibility.
        """
        session = self._sessions.get(session_id)
        if session and session.is_alive():
            # Inject command visually into the PTY shell
            session.inject_command(f"# Claude: {command}")
            session.inject_command(command)

        # Actually execute and capture output via subprocess for tool result
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace_dir),
                env={**os.environ, "TERM": "dumb"},
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return {
                    "stdout": "",
                    "stderr": f"Command timed out after {timeout} seconds",
                    "returncode": -1,
                }

            stdout_str = stdout.decode("utf-8", errors="replace")[:16000]
            stderr_str = stderr.decode("utf-8", errors="replace")[:4000]

            return {
                "stdout": stdout_str,
                "stderr": stderr_str,
                "returncode": proc.returncode,
            }
        except Exception as e:
            return {
                "stdout": "",
                "stderr": str(e),
                "returncode": -1,
            }

    async def handle_websocket(self, websocket: WebSocket, session_id: str):
        """Handle WebSocket connection for a shell session."""
        loop = asyncio.get_event_loop()
        session = self.get_or_create_session(session_id, loop)

        # Send initial data
        await websocket.send_text(json.dumps({
            "type": "connected",
            "session_id": session_id,
        }))

        async def send_output():
            """Continuously send PTY output to WebSocket."""
            while True:
                try:
                    data = await session.get_output(timeout=0.05)
                    if data:
                        await websocket.send_bytes(data)
                    else:
                        if not session.is_alive():
                            # Restart session
                            new_session = ShellSession(session_id, self.workspace_dir)
                            new_session.start(loop)
                            self._sessions[session_id] = session
                            await asyncio.sleep(0.5)
                except Exception:
                    break

        output_task = asyncio.create_task(send_output())

        try:
            while True:
                try:
                    msg = await websocket.receive()
                except Exception:
                    break

                if "bytes" in msg and msg["bytes"]:
                    session.write(msg["bytes"])
                elif "text" in msg and msg["text"]:
                    try:
                        data = json.loads(msg["text"])
                        if data.get("type") == "resize":
                            rows = int(data.get("rows", 24))
                            cols = int(data.get("cols", 80))
                            session.resize(rows, cols)
                    except (json.JSONDecodeError, ValueError):
                        session.write(msg["text"].encode("utf-8"))
        finally:
            output_task.cancel()
            try:
                await output_task
            except asyncio.CancelledError:
                pass
