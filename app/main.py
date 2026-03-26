import os
import json
import uuid
import asyncio
import threading
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, UploadFile, File, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

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


def _start_tmate_bg():
    """Init tmate session in background so app starts fast."""
    print("[tmate] Starting session...", flush=True)
    try:
        s = shell_manager.get_or_create_session()
        if s.web_url:
            print(f"[tmate] ✓ Session ready", flush=True)
            print(f"[tmate] Web (read-only): {s.web_url}", flush=True)
            if s.ssh_url:
                print(f"[tmate] SSH (read-only): {s.ssh_url}", flush=True)
        else:
            print("[tmate] ✗ Session started but no URL received (check internet access to tmate.io)", flush=True)
    except Exception as e:
        print(f"[tmate] ✗ Init error: {e}", flush=True)


@app.on_event("startup")
async def startup():
    t = threading.Thread(target=_start_tmate_bg, daemon=True)
    t.start()


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


@app.get("/tmate-url")
async def tmate_url(_=Depends(basic_auth)):
    s = shell_manager._session
    if s and s.web_url:
        return {"url": s.web_url, "ssh": s.ssh_url, "ready": True}
    return {"url": None, "ready": False}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, _=Depends(basic_auth)):
    locale = get_locale(request)
    t = get_translations(locale)
    chats = storage.list_chats()
    tmate_url = shell_manager.get_web_url()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "t": t,
        "locale": locale,
        "chats": chats,
        "supported_locales": SUPPORTED_LOCALES,
        "tmate_url": tmate_url or "",
    })


@app.post("/locale/{locale}")
async def set_locale(locale: str, _=Depends(basic_auth)):
    if locale not in SUPPORTED_LOCALES:
        raise HTTPException(status_code=400, detail="Unsupported locale")
    resp = JSONResponse({"ok": True})
    resp.set_cookie("locale", locale, max_age=60*60*24*365)
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
    body = await request.json()
    user_message = body.get("message", "").strip()
    if not user_message:
        raise HTTPException(status_code=400, detail="Empty message")
    messages = storage.load_chat(chat_id) or []
    messages.append({"role": "user", "content": user_message})
    try:
        reply, tool_uses = await handle_chat_message(
            messages=messages,
            workspace_dir=WORKSPACE_DIR,
            shell_manager=shell_manager,
            chat_id=chat_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    messages.append({"role": "assistant", "content": reply})
    storage.save_chat(chat_id, messages)
    return {"reply": reply, "tool_uses": tool_uses, "messages": messages}


@app.websocket("/ws/shell/{session_id}")
async def shell_ws(websocket: WebSocket, session_id: str):
    await websocket.accept()
    try:
        # Send tmate URL to client
        s = shell_manager._session
        await websocket.send_text(json.dumps({
            "type": "tmate",
            "web_url": s.web_url if s else None,
            "ssh_url": s.ssh_url if s else None,
        }) if True else "{}")
        while True:
            await asyncio.sleep(10)
            s = shell_manager._session
            await websocket.send_text(json.dumps({
                "type": "tmate",
                "web_url": s.web_url if s else None,
            }))
    except Exception:
        pass
