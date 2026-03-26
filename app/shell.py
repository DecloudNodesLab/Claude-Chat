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
    Exposes SSH read-only URL for browser terminal via asyncssh.
    """

    def __init__(self, session_name: str, workspace_dir: Path):
        self.session_name = session_name
        self.workspace_dir = workspace_dir
        self.web_url: Optional[str] = None
        self.ssh_ro: Optional[str] = None   # full string: "ssh TOKEN@host"
        self.ssh_host: Optional[str] = None
        self.ssh_user: Optional[str] = None
        self._sock: str = f"/tmp/tmate-{session_name}.sock"
        self._ready = False

    def start(self) -> bool:
        env = os.environ.copy()
        env["HOME"] = "/root"
        for k in ("ANTHROPIC_API_KEY", "BASIC_AUTH_PASSWORD", "APP_SECRET_KEY"):
            env.pop(k, None)

        # Kill old session
        subprocess.run(["tmate", "-S", self._sock, "kill-session"],
                       capture_output=True)
        time.sleep(0.3)

        # Start new detached session
        r = subprocess.run(
            ["tmate", "-S", self._sock, "new-session", "-d",
             "-s", self.session_name, "-x", "220", "-y", "50"],
            env=env, cwd=str(self.workspace_dir),
            capture_output=True,
        )

        # Wait for SSH URL
        print("[tmate] Waiting for SSH URL from tmate.io...", flush=True)
        for attempt in range(40):
            time.sleep(0.5)
            result = subprocess.run(
                ["tmate", "-S", self._sock, "display", "-p",
                 "#{tmate_ssh_ro}"],
                capture_output=True, text=True,
            )
            out = result.stdout.strip()
            if out and "@" in out and "tmate.io" in out:
                self.ssh_ro = out  # e.g. "ssh ro-xxxx@lon1.tmate.io"
                # Parse user and host
                # format: "ssh USER@HOST"
                parts = out.replace("ssh ", "").strip().split("@")
                if len(parts) == 2:
                    self.ssh_user = parts[0]
                    self.ssh_host = parts[1]
                self._ready = True
                print(f"[tmate] SSH (read-only): {out}", flush=True)
                break
            if attempt > 0 and attempt % 6 == 0:
                print(f"[tmate] Still waiting... ({attempt}/40)", flush=True)

        if not self._ready:
            print("[tmate] ✗ No SSH URL received. Check internet access to tmate.io", flush=True)
            return False

        # cd to workspace
        subprocess.run(
            ["tmate", "-S", self._sock, "send-keys", "-t",
             self.session_name, f"cd {self.workspace_dir} && clear", "Enter"],
            capture_output=True,
        )
        return True

    def inject_command(self, command: str):
        subprocess.run(
            ["tmate", "-S", self._sock, "send-keys", "-t",
             self.session_name, command, "Enter"],
            capture_output=True,
        )

    def is_alive(self) -> bool:
        r = subprocess.run(
            ["tmate", "-S", self._sock, "list-sessions"],
            capture_output=True,
        )
        return r.returncode == 0

    def stop(self):
        subprocess.run(["tmate", "-S", self._sock, "kill-session"],
                       capture_output=True)


class ShellManager:
    def __init__(self, workspace_dir: Path):
        self.workspace_dir = workspace_dir
        self._session: Optional[TmateSession] = None
        self._init_lock = threading.Lock()

    def get_or_create_session(self) -> TmateSession:
        with self._init_lock:
            if self._session is None or not self._session.is_alive():
                s = TmateSession("claude", self.workspace_dir)
                s.start()
                self._session = s
            return self._session

    def get_ssh_info(self):
        s = self._session
        if s and s._ready:
            return {"host": s.ssh_host, "user": s.ssh_user, "ro": s.ssh_ro}
        return None

    async def run_command_in_session(
        self, command: str, session_id: str = "default", timeout: float = 30.0
    ) -> dict:
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

    async def handle_websocket(self, websocket: WebSocket, session_id: str):
        """
        Connect to tmate SSH read-only session via asyncssh
        and stream output to browser xterm.js over WebSocket.
        """
        import asyncssh

        s = self._session
        if not s or not s._ready or not s.ssh_host:
            await websocket.send_text(json.dumps({
                "type": "error",
                "msg": "tmate session not ready yet, please wait...",
            }))
            await websocket.close()
            return

        await websocket.send_text(json.dumps({"type": "connected"}))

        try:
            async with asyncssh.connect(
                s.ssh_host,
                port=22,
                username=s.ssh_user,
                known_hosts=None,        # read-only token auth, no password needed
                password="",
                client_keys=[],
                preferred_auth=["password"],
            ) as conn:
                async with conn.create_process(
                    term_type="xterm-256color",
                    term_size=(80, 24),
                ) as proc:

                    async def ws_to_ssh():
                        """Forward browser keystrokes to SSH."""
                        while True:
                            try:
                                msg = await websocket.receive()
                                if msg.get("type") == "websocket.disconnect":
                                    proc.stdin.write_eof()
                                    return
                                raw = msg.get("bytes") or (
                                    msg.get("text", "").encode() if msg.get("text") else None
                                )
                                if not raw:
                                    continue
                                # Resize control message
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
                                # It's a read-only session — ignore actual keystrokes
                                # (tmate ro sessions don't accept input)
                            except Exception:
                                return

                    async def ssh_to_ws():
                        """Forward SSH output to browser."""
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

        except asyncssh.DisconnectError as e:
            try:
                await websocket.send_text(json.dumps({"type": "error", "msg": str(e)}))
            except Exception:
                pass
        except Exception as e:
            try:
                await websocket.send_text(json.dumps({"type": "error", "msg": str(e)}))
            except Exception:
                pass
