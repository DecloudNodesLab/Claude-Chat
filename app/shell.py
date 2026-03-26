import os
import asyncio
import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import WebSocket


class TmateSession:
    def __init__(self, workspace_dir: Path):
        self.workspace_dir = workspace_dir
        self.ssh_ro: Optional[str] = None
        self.ssh_host: Optional[str] = None
        self.ssh_user: Optional[str] = None
        self._sock = "/tmp/tmate-claude.sock"
        self._ready = False

    def start(self) -> bool:
        env = os.environ.copy()
        env["HOME"] = "/root"
        for k in ("ANTHROPIC_API_KEY", "BASIC_AUTH_PASSWORD", "APP_SECRET_KEY"):
            env.pop(k, None)

        # Kill old session silently
        subprocess.run(["tmate", "-S", self._sock, "kill-session"],
                       capture_output=True)
        time.sleep(0.3)

        # Start new detached session
        subprocess.run(
            ["tmate", "-S", self._sock, "new-session", "-d",
             "-s", "main", "-x", "220", "-y", "50"],
            env=env, cwd=str(self.workspace_dir),
            capture_output=True,
        )

        # Wait for SSH URL (up to 20 seconds)
        print("[tmate] Waiting for SSH URL from tmate.io...", flush=True)
        for attempt in range(40):
            time.sleep(0.5)
            r = subprocess.run(
                ["tmate", "-S", self._sock, "display", "-p", "#{tmate_ssh_ro}"],
                capture_output=True, text=True,
            )
            out = r.stdout.strip()
            if out and "@" in out and "tmate.io" in out:
                self.ssh_ro = out  # "ssh ro-TOKEN@lon1.tmate.io"
                token_host = out.replace("ssh ", "").strip()
                parts = token_host.split("@")
                if len(parts) == 2:
                    self.ssh_user = parts[0]
                    self.ssh_host = parts[1]
                self._ready = True
                print(f"[tmate] ✓ SSH (read-only): {out}", flush=True)
                break
            if attempt > 0 and attempt % 6 == 0:
                print(f"[tmate] Still waiting... ({attempt}/40)", flush=True)

        if not self._ready:
            print("[tmate] ✗ No URL received — check internet to tmate.io port 22", flush=True)
            return False

        # cd to workspace
        subprocess.run(
            ["tmate", "-S", self._sock, "send-keys", "-t", "main",
             f"cd {self.workspace_dir} && clear", "Enter"],
            capture_output=True,
        )
        return True

    def inject_command(self, command: str):
        if self._ready:
            subprocess.run(
                ["tmate", "-S", self._sock, "send-keys", "-t", "main",
                 command, "Enter"],
                capture_output=True,
            )

    def is_alive(self) -> bool:
        r = subprocess.run(["tmate", "-S", self._sock, "list-sessions"],
                           capture_output=True)
        return r.returncode == 0


class ShellManager:
    def __init__(self, workspace_dir: Path):
        self.workspace_dir = workspace_dir
        self._session: Optional[TmateSession] = None
        self._lock = threading.Lock()

    def get_or_create_session(self) -> TmateSession:
        with self._lock:
            if self._session is None or not self._session.is_alive():
                s = TmateSession(self.workspace_dir)
                s.start()
                self._session = s
            return self._session

    # Called by tools.py — session_id kept for API compat but ignored
    async def run_command_in_session(
        self,
        command: str,
        session_id: str = "default",
        timeout: float = 30.0,
    ) -> dict:
        s = self._session
        if s and s._ready:
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
                return {"stdout": "", "stderr": f"Timed out after {int(timeout)}s",
                        "returncode": -1}
            return {
                "stdout": out.decode("utf-8", errors="replace")[:16000],
                "stderr": err.decode("utf-8", errors="replace")[:4000],
                "returncode": proc.returncode,
            }
        except Exception as e:
            return {"stdout": "", "stderr": str(e), "returncode": -1}

    async def handle_websocket(self, websocket: WebSocket, session_id: str):
        """Stream tmate read-only SSH session to browser via asyncssh."""
        import asyncssh

        # Wait up to 15s for tmate to be ready
        for _ in range(30):
            s = self._session
            if s and s._ready and s.ssh_host:
                break
            await websocket.send_text(json.dumps({
                "type": "info",
                "msg": "Waiting for tmate session...",
            }))
            await asyncio.sleep(0.5)
        else:
            await websocket.send_text(json.dumps({
                "type": "error",
                "msg": "tmate session not ready. Check container logs.",
            }))
            return

        s = self._session
        await websocket.send_text(json.dumps({"type": "connected"}))

        try:
            async with asyncssh.connect(
                s.ssh_host,
                port=22,
                username=s.ssh_user,
                known_hosts=None,
                # tmate ro tokens use keyboard-interactive / none auth
                client_keys=[],
                password=None,
                preferred_auth=("none", "keyboard-interactive", "password"),
            ) as conn:

                async with conn.create_process(
                    request_pty=True,
                    term_type="xterm-256color",
                    term_size=(220, 50),
                ) as proc:

                    async def ws_to_ssh():
                        while True:
                            try:
                                msg = await websocket.receive()
                            except Exception:
                                return
                            if msg.get("type") == "websocket.disconnect":
                                return
                            raw = msg.get("bytes") or (
                                msg.get("text", "").encode()
                                if msg.get("text") else None
                            )
                            if not raw:
                                continue
                            if raw[0:1] == b"{":
                                try:
                                    obj = json.loads(raw)
                                    if obj.get("type") == "resize":
                                        proc.change_terminal_size(
                                            int(obj["cols"]), int(obj["rows"])
                                        )
                                        continue
                                except Exception:
                                    pass
                            # read-only session — input is ignored by tmate
                            # but we still forward resize messages

                    async def ssh_to_ws():
                        try:
                            while True:
                                data = await proc.stdout.read(4096)
                                if not data:
                                    break
                                if isinstance(data, str):
                                    data = data.encode("utf-8", errors="replace")
                                await websocket.send_bytes(data)
                        except Exception:
                            pass

                    await asyncio.gather(ws_to_ssh(), ssh_to_ws())

        except Exception as e:
            try:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "msg": f"SSH error: {e}",
                }))
            except Exception:
                pass
