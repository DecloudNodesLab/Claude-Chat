import os
import asyncio
import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from fastapi import WebSocket


class TmateSession:
    """
    Manages a tmate session.
    - Starts tmate, reads the web read-only URL
    - Provides inject_command via `tmate send-keys`
    - Provides run_command for Claude tools via subprocess
    """

    def __init__(self, session_name: str, workspace_dir: Path):
        self.session_name = session_name
        self.workspace_dir = workspace_dir
        self.web_url: Optional[str] = None
        self.ssh_url: Optional[str] = None
        self._ready = False
        self._proc: Optional[subprocess.Popen] = None

    def start(self):
        """Start tmate session and wait for URLs."""
        env = os.environ.copy()
        env["HOME"] = str(self.workspace_dir)
        for k in ("ANTHROPIC_API_KEY", "BASIC_AUTH_PASSWORD", "APP_SECRET_KEY"):
            env.pop(k, None)

        # Kill existing session if any
        subprocess.run(
            ["tmate", "-S", f"/tmp/tmate-{self.session_name}.sock", "kill-session"],
            capture_output=True,
        )
        time.sleep(0.3)

        sock = f"/tmp/tmate-{self.session_name}.sock"

        # Start tmate with a named session
        self._proc = subprocess.Popen(
            ["tmate", "-S", sock, "new-session", "-d", "-s", self.session_name, "-x", "220", "-y", "50"],
            env=env,
            cwd=str(self.workspace_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._proc.wait()

        # Wait for tmate to be ready and fetch URLs
        for _ in range(30):
            time.sleep(0.5)
            result = subprocess.run(
                ["tmate", "-S", sock, "display", "-p",
                 "#{tmate_web_ro} #{tmate_ssh_ro}"],
                capture_output=True, text=True,
            )
            out = result.stdout.strip()
            if out and "https://" in out:
                parts = out.split()
                for p in parts:
                    if p.startswith("https://"):
                        self.web_url = p
                    elif p.startswith("ssh "):
                        self.ssh_url = p
                if self.web_url:
                    self._ready = True
                    break

        # cd to workspace inside the session
        self._send_keys(f"cd {self.workspace_dir} && clear")
        return self._ready

    def _send_keys(self, keys: str):
        sock = f"/tmp/tmate-{self.session_name}.sock"
        subprocess.run(
            ["tmate", "-S", sock, "send-keys", "-t", self.session_name, keys, "Enter"],
            capture_output=True,
        )

    def inject_command(self, command: str):
        """Send a command to the tmate session."""
        self._send_keys(command)

    def is_alive(self) -> bool:
        sock = f"/tmp/tmate-{self.session_name}.sock"
        r = subprocess.run(
            ["tmate", "-S", sock, "list-sessions"],
            capture_output=True,
        )
        return r.returncode == 0

    def stop(self):
        sock = f"/tmp/tmate-{self.session_name}.sock"
        subprocess.run(
            ["tmate", "-S", sock, "kill-session"],
            capture_output=True,
        )


class ShellManager:
    def __init__(self, workspace_dir: Path):
        self.workspace_dir = workspace_dir
        self._session: Optional[TmateSession] = None
        self._init_lock = threading.Lock()

    def get_or_create_session(self) -> TmateSession:
        with self._init_lock:
            if self._session is None or not self._session.is_alive():
                s = TmateSession("claude", self.workspace_dir)
                ok = s.start()
                if not ok:
                    # tmate failed (no internet?) — try again without network
                    # fallback: still return session, URLs will be None
                    pass
                self._session = s
            return self._session

    def get_web_url(self) -> Optional[str]:
        if self._session:
            return self._session.web_url
        return None

    async def run_command_in_session(
        self, command: str, session_id: str = "default", timeout: float = 30.0
    ) -> dict:
        """Run command for Claude tool. Show in tmate + capture output."""
        s = self._session
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

    # WebSocket handle kept for compatibility but not used for terminal display
    async def handle_websocket(self, websocket: WebSocket, session_id: str):
        await websocket.accept() if not websocket.client_state.value == 1 else None
        await websocket.send_text(json.dumps({
            "type": "tmate",
            "web_url": self.get_web_url(),
        }))
        # Keep connection alive
        try:
            while True:
                await asyncio.sleep(30)
                await websocket.send_text(json.dumps({"type": "ping"}))
        except Exception:
            pass
