import json
import os
from pathlib import Path
from typing import List, Dict, Optional


class Storage:
    """Simple JSON-file based storage for chats."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.chats_dir = data_dir / "chats"
        self.chats_dir.mkdir(parents=True, exist_ok=True)

    def _chat_path(self, chat_id: str) -> Path:
        # Sanitize chat_id
        safe_id = "".join(c for c in chat_id if c.isalnum() or c in "-_")
        return self.chats_dir / f"{safe_id}.json"

    def list_chats(self) -> List[Dict]:
        chats = []
        for f in sorted(self.chats_dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            chat_id = f.stem
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                messages = data if isinstance(data, list) else []
                preview = ""
                for msg in messages:
                    if msg.get("role") == "user":
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            preview = content[:60]
                            if len(content) > 60:
                                preview += "..."
                            break
                chats.append({
                    "id": chat_id,
                    "preview": preview or "Empty chat",
                    "message_count": len(messages),
                })
            except Exception:
                pass
        return chats

    def load_chat(self, chat_id: str) -> Optional[List[Dict]]:
        path = self._chat_path(chat_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def save_chat(self, chat_id: str, messages: List[Dict]):
        path = self._chat_path(chat_id)
        path.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")

    def delete_chat(self, chat_id: str):
        path = self._chat_path(chat_id)
        if path.exists():
            path.unlink()
