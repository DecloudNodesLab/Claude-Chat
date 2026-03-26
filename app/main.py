import os
import json
import uuid
import asyncio
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, UploadFile, File, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from jinja2 import Environment as JinjaEnv

from app.auth import basic_auth
from app.chat import handle_chat_message
from app.shell import ShellManager
from app.storage import Storage
from app.i18n import get_translations, SUPPORTED_LOCALES

WORKSPACE_DIR = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Claude Workspace", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

templates = Jinja2Templates(directory="templates")
storage = Storage(DATA_DIR)
shell_manager = ShellManager(WORKSPACE_DIR)

# In-memory task store: task_id -> {status, result, error}
_tasks: dict = {}


def get_locale(request: Request) -> str:
    c = request.cookies.get("locale")
    if c and c in SUPPORTED_LOCALES:
        return c
    d = os.environ.get("DEFAULT_LOCALE", "en")
    return d if d in SUPPORTED_LOCALES else "en"


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    return response


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/style.css")
async def serve_css():
    """Serve style.css from workspace (editable) or templates fallback."""
    workspace_css = WORKSPACE_DIR / "style.css"
    if workspace_css.exists():
        css = workspace_css.read_text(encoding="utf-8")
    else:
        fallback = Path("templates") / "style.css"
        css = fallback.read_text(encoding="utf-8") if fallback.exists() else ""
    return Response(content=css, media_type="text/css")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, _=Depends(basic_auth)):
    locale = get_locale(request)
    t = get_translations(locale)
    chats = storage.list_chats()
    workspace_index = WORKSPACE_DIR / "index.html"
    if workspace_index.exists():
        src = workspace_index.read_text(encoding="utf-8")
        env = JinjaEnv(autoescape=False)
        html = env.from_string(src).render(
            t=t, locale=locale, chats=chats,
            supported_locales=SUPPORTED_LOCALES,
        )
        return HTMLResponse(content=html)
    return templates.TemplateResponse("index.html", {
        "request": request, "t": t, "locale": locale,
        "chats": chats, "supported_locales": SUPPORTED_LOCALES,
    })


@app.post("/locale/{locale}")
async def set_locale(locale: str, _=Depends(basic_auth)):
    if locale not in SUPPORTED_LOCALES:
        raise HTTPException(status_code=400, detail="Unsupported locale")
    resp = JSONResponse({"ok": True})
    resp.set_cookie("locale", locale, max_age=60 * 60 * 24 * 365)
    return resp


@app.post("/upload")
async def upload_file(file: UploadFile = File(...), _=Depends(basic_auth)):
    filename = Path(file.filename).name
    if not filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    dest = (WORKSPACE_DIR / filename).resolve()
    if not str(dest).startswith(str(WORKSPACE_DIR.resolve())):
        raise HTTPException(status_code=400, detail="Invalid path")
    content = await file.read()
    dest.write_bytes(content)
    return JSONResponse({"ok": True, "filename": filename, "size": len(content)})


@app.get("/chats")
async def list_chats(_=Depends(basic_auth)):
    return storage.list_chats()


@app.post("/chats")
async def create_chat(_=Depends(basic_auth)):
    chat_id = str(uuid.uuid4())
    storage.save_chat(chat_id, [])
    return {"id": chat_id, "messages": []}


@app.get("/chats/{chat_id}")
async def get_chat(chat_id: str, _=Depends(basic_auth)):
    messages = storage.load_chat(chat_id)
    if messages is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return {"id": chat_id, "messages": messages}


@app.delete("/chats/{chat_id}")
async def delete_chat(chat_id: str, _=Depends(basic_auth)):
    storage.delete_chat(chat_id)
    return {"ok": True}


@app.post("/chats/{chat_id}/message")
async def send_message(chat_id: str, request: Request, _=Depends(basic_auth)):
    """
    Start Claude task and return task_id immediately.
    Client polls /tasks/{task_id} to get result.
    This prevents proxy timeout on long requests.
    """
    body = await request.json()
    user_message = body.get("message", "").strip()
    if not user_message:
        raise HTTPException(status_code=400, detail="Empty message")

    messages = storage.load_chat(chat_id) or []
    messages.append({"role": "user", "content": user_message})

    task_id = str(uuid.uuid4())
    _tasks[task_id] = {"status": "running"}

    async def run():
        try:
            reply, tool_uses = await handle_chat_message(
                messages=messages,
                workspace_dir=WORKSPACE_DIR,
                shell_manager=shell_manager,
                chat_id=chat_id,
                data_dir=DATA_DIR,
            )
            messages.append({"role": "assistant", "content": reply})
            storage.save_chat(chat_id, messages)
            _tasks[task_id] = {
                "status": "done",
                "reply": reply,
                "tool_uses": tool_uses,
                "messages": messages,
            }
        except Exception as e:
            _tasks[task_id] = {"status": "error", "error": str(e)}

    asyncio.create_task(run())
    return {"task_id": task_id}


@app.get("/tasks/{task_id}")
async def get_task(task_id: str, _=Depends(basic_auth)):
    """Poll task status. Returns running/done/error."""
    task = _tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    # Clean up completed tasks after reading
    if task["status"] in ("done", "error"):
        _tasks.pop(task_id, None)
    return task


@app.get("/files")
async def list_files_api(path: str = "", _=Depends(basic_auth)):
    """List files and directories in workspace."""
    import stat as stat_mod
    base = WORKSPACE_DIR.resolve()
    target = (base / path).resolve() if path else base
    if not str(target).startswith(str(base)):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not target.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    items = []
    try:
        for entry in sorted(target.iterdir(), key=lambda e: (e.is_file(), e.name.lower())):
            rel = str(entry.relative_to(base))
            st = entry.stat()
            items.append({
                "name": entry.name,
                "path": rel,
                "is_dir": entry.is_dir(),
                "size": st.st_size if entry.is_file() else 0,
                "modified": int(st.st_mtime),
            })
    except PermissionError:
        pass
    current_rel = str(target.relative_to(base)) if target != base else ""
    parent_rel = str(target.parent.relative_to(base)) if target != base else None
    return {"items": items, "current": current_rel, "parent": parent_rel}


@app.get("/files/download")
async def download_file(path: str, _=Depends(basic_auth)):
    """Download a file from workspace."""
    from fastapi.responses import FileResponse
    base = WORKSPACE_DIR.resolve()
    target = (base / path).resolve()
    if not str(target).startswith(str(base)):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path=str(target), filename=target.name)


@app.delete("/files")
async def delete_file(path: str, _=Depends(basic_auth)):
    """Delete a file or directory from workspace."""
    import shutil
    base = WORKSPACE_DIR.resolve()
    target = (base / path).resolve()
    if not str(target).startswith(str(base)):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not target.exists():
        raise HTTPException(status_code=404, detail="Not found")
    if target.is_file():
        target.unlink()
    else:
        shutil.rmtree(target)
    return {"ok": True}


@app.post("/files/upload")
async def upload_to_path(path: str = "", file: UploadFile = File(...), _=Depends(basic_auth)):
    """Upload a file to a specific workspace directory."""
    base = WORKSPACE_DIR.resolve()
    dest_dir = (base / path).resolve() if path else base
    if not str(dest_dir).startswith(str(base)):
        raise HTTPException(status_code=400, detail="Invalid path")
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(file.filename).name
    dest = dest_dir / filename
    data = await file.read()
    dest.write_bytes(data)
    return {"ok": True, "filename": filename, "size": len(data)}


# ── Settings API ──────────────────────────────────────────────────────────────
@app.get("/settings")
async def get_settings(_=Depends(basic_auth)):
    path = DATA_DIR / "settings.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"), "system_prompt": ""}


@app.post("/settings")
async def save_settings(request: Request, _=Depends(basic_auth)):
    body = await request.json()
    path = DATA_DIR / "settings.json"
    # Merge with existing
    existing = {}
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
    existing.update({k: v for k, v in body.items() if k in ("model", "system_prompt")})
    path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True}


@app.get("/models")
async def list_models(_=Depends(basic_auth)):
    """Return available Claude models with pricing."""
    models = [
        {"id": "claude-opus-4-6",           "name": "Claude Opus 4.6",    "input": 5.00,  "output": 25.00, "badge": "powerful",    "ctx": "1M"},
        {"id": "claude-sonnet-4-6",          "name": "Claude Sonnet 4.6",  "input": 3.00,  "output": 15.00, "badge": "recommended", "ctx": "1M"},
        {"id": "claude-opus-4-5-20251101",   "name": "Claude Opus 4.5",    "input": 5.00,  "output": 25.00, "badge": "powerful",    "ctx": "200K"},
        {"id": "claude-sonnet-4-5-20250929", "name": "Claude Sonnet 4.5",  "input": 3.00,  "output": 15.00, "badge": "recommended", "ctx": "200K"},
        {"id": "claude-haiku-4-5-20251001",  "name": "Claude Haiku 4.5",   "input": 1.00,  "output": 5.00,  "badge": "fast",        "ctx": "200K"},
        {"id": "claude-opus-4-1-20250805",   "name": "Claude Opus 4.1",    "input": 15.00, "output": 75.00, "badge": "",            "ctx": "200K"},
        {"id": "claude-sonnet-4-20250514",   "name": "Claude Sonnet 4",    "input": 3.00,  "output": 15.00, "badge": "",            "ctx": "200K"},
    ]
    return models


@app.websocket("/ws/shell/{session_id}")
async def shell_ws(websocket: WebSocket, session_id: str):
    safe_id = "".join(c for c in session_id if c.isalnum() or c in "-_") or "default"
    await websocket.accept()
    try:
        await shell_manager.handle_websocket(websocket, safe_id)
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await websocket.close()
        except Exception:
            pass
