import os
import secrets
import base64
from fastapi import Request, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials

BASIC_AUTH_USERNAME = os.environ.get("BASIC_AUTH_USERNAME", "admin")
BASIC_AUTH_PASSWORD = os.environ.get("BASIC_AUTH_PASSWORD", "changeme")

security = HTTPBasic()


async def basic_auth(request: Request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Basic "):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic realm=\"Claude Workspace\""},
        )
    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
        username, _, password = decoded.partition(":")
    except Exception:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic realm=\"Claude Workspace\""},
        )

    ok_user = secrets.compare_digest(username, BASIC_AUTH_USERNAME)
    ok_pass = secrets.compare_digest(password, BASIC_AUTH_PASSWORD)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic realm=\"Claude Workspace\""},
        )
    return username
