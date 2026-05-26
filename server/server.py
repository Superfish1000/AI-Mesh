"""
AI Mesh Coordination Server
FastAPI + WebSockets + SQLite + JWT auth + API keys + embedded web GUI
"""

import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import bcrypt
from fastapi import Cookie, FastAPI, Form, HTTPException, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from jose import JWTError, jwt
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

DB_PATH     = Path(__file__).parent / "mesh.db"
CLIENT_PATH = Path(__file__).parent.parent / "client" / "mcp_client.py"
SECRET_FILE = Path(__file__).parent / "jwt_secret.txt"

JWT_ALGORITHM    = "HS256"
JWT_EXPIRE_HOURS = 24
WS_TOKEN_EXPIRE_MINUTES = 60

# Load or generate JWT secret
if SECRET_FILE.exists():
    JWT_SECRET = SECRET_FILE.read_text().strip()
else:
    JWT_SECRET = secrets.token_hex(32)
    SECRET_FILE.write_text(JWT_SECRET)
    print(f"[AI Mesh] Generated JWT secret → {SECRET_FILE}")

# Bootstrap state (first-run admin setup)
_bootstrap_token: Optional[str] = None
_bootstrap_expiry: float = 0.0

# ---------------------------------------------------------------------------
# WebSocket managers
# ---------------------------------------------------------------------------

class InstanceManager:
    def __init__(self):
        self._sockets: dict[str, WebSocket] = {}

    async def connect(self, instance_id: str, ws: WebSocket):
        await ws.accept()
        self._sockets[instance_id] = ws

    def disconnect(self, instance_id: str):
        self._sockets.pop(instance_id, None)

    async def send(self, instance_id: str, data: dict) -> bool:
        ws = self._sockets.get(instance_id)
        if ws:
            try:
                await ws.send_json(data)
                return True
            except Exception:
                self.disconnect(instance_id)
        return False

    async def broadcast(self, data: dict, exclude: Optional[str] = None):
        dead = []
        for iid, ws in list(self._sockets.items()):
            if iid == exclude:
                continue
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(iid)
        for iid in dead:
            self.disconnect(iid)


class AdminManager:
    def __init__(self):
        self._sockets: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._sockets.append(ws)

    def disconnect(self, ws: WebSocket):
        try:
            self._sockets.remove(ws)
        except ValueError:
            pass

    async def broadcast(self, data: dict):
        dead = []
        for ws in list(self._sockets):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


instances_ws = InstanceManager()
admins_ws    = AdminManager()

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    global _bootstrap_token, _bootstrap_expiry
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id            TEXT PRIMARY KEY,
                username      TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin      INTEGER DEFAULT 0,
                created_at    REAL
            );

            CREATE TABLE IF NOT EXISTS api_keys (
                id          TEXT PRIMARY KEY,
                key_hash    TEXT NOT NULL,
                key_prefix  TEXT NOT NULL,
                owner_id    TEXT NOT NULL,
                label       TEXT DEFAULT '',
                created_at  REAL,
                last_used   REAL,
                revoked     INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS instances (
                id             TEXT PRIMARY KEY,
                name           TEXT NOT NULL,
                display_name   TEXT,
                instance_type  TEXT DEFAULT 'claude-code',
                system_info    TEXT DEFAULT '{}',
                notes          TEXT DEFAULT '',
                owner_id       TEXT,
                visibility     TEXT DEFAULT 'private',
                last_seen      REAL,
                created_at     REAL,
                connected      INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS messages (
                id        TEXT PRIMARY KEY,
                from_id   TEXT NOT NULL,
                to_id     TEXT,
                content   TEXT NOT NULL,
                timestamp REAL NOT NULL,
                read      INTEGER DEFAULT 0
            );
        """)

        # Backward-compat: add visibility column to existing instances table
        try:
            conn.execute("ALTER TABLE instances ADD COLUMN visibility TEXT DEFAULT 'private'")
        except sqlite3.OperationalError:
            pass  # column already exists

        # Bootstrap: if no users exist, generate a one-time setup token
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    if count == 0:
        _bootstrap_token  = secrets.token_urlsafe(32)
        _bootstrap_expiry = time.time() + 600  # 10 minutes
        print("\n" + "="*60)
        print("  AI Mesh — First Run Setup")
        print("="*60)
        print(f"  No admin account found. Create one at:")
        print(f"  /setup?token={_bootstrap_token}")
        print(f"  (expires in 10 minutes)")
        print("="*60 + "\n")


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task1 = asyncio.create_task(_cleanup_loop())
    task2 = asyncio.create_task(_ping_loop())
    yield
    task1.cancel()
    task2.cancel()


async def _cleanup_loop():
    while True:
        await asyncio.sleep(15)
        threshold = time.time() - 30
        with db() as conn:
            rows = conn.execute(
                "SELECT id FROM instances WHERE last_seen < ? AND connected = 1",
                (threshold,),
            ).fetchall()
            if rows:
                conn.execute(
                    "UPDATE instances SET connected = 0 WHERE last_seen < ? AND connected = 1",
                    (threshold,),
                )
        for row in rows:
            await admins_ws.broadcast({"type": "instance_disconnected", "id": row["id"]})


async def _ping_loop():
    while True:
        await asyncio.sleep(20)
        await instances_ws.broadcast({"type": "ping"})


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="AI Mesh", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False


def _hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _make_jwt(payload: dict, expire_minutes: int = JWT_EXPIRE_HOURS * 60) -> str:
    data = {**payload, "exp": time.time() + expire_minutes * 60}
    return jwt.encode(data, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _decode_jwt(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])


def _session_user(mesh_session: Optional[str]) -> Optional[dict]:
    if not mesh_session:
        return None
    try:
        payload = _decode_jwt(mesh_session)
        with db() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (payload["sub"],)
            ).fetchone()
        return dict(row) if row else None
    except (JWTError, Exception):
        return None


async def require_session(mesh_session: Optional[str] = Cookie(default=None)) -> dict:
    user = _session_user(mesh_session)
    if not user:
        raise HTTPException(401, "Login required")
    return user


async def require_admin(mesh_session: Optional[str] = Cookie(default=None)) -> dict:
    user = _session_user(mesh_session)
    if not user:
        raise HTTPException(401, "Login required")
    if not user["is_admin"]:
        raise HTTPException(403, "Admin access required")
    return user


def _resolve_api_key(x_api_key: Optional[str]) -> Optional[dict]:
    """Validate an API key header and return the api_keys row, or None."""
    if not x_api_key or not x_api_key.startswith("mesh_"):
        return None
    key_prefix = x_api_key[:13]  # "mesh_" + 8 hex chars
    key_hash   = _hash_api_key(x_api_key)
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM api_keys WHERE key_prefix = ? AND key_hash = ? AND revoked = 0",
            (key_prefix, key_hash),
        ).fetchone()
    if not row:
        return None
    with db() as conn:
        conn.execute(
            "UPDATE api_keys SET last_used = ? WHERE id = ?",
            (time.time(), row["id"]),
        )
    return dict(row)


async def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> dict:
    key_row = _resolve_api_key(x_api_key)
    if not key_row:
        raise HTTPException(401, "Valid X-API-Key required (format: mesh_...)")
    return key_row


def _user_can_see_instance(user: dict, inst: dict) -> bool:
    """A user can see their own instances, any public instance, or all if admin."""
    if user.get("is_admin"):
        return True
    if inst.get("owner_id") == user["id"]:
        return True
    return inst.get("visibility") == "public"


def _instance_messageable_by(sender_owner_id: str, target: dict, sender_is_admin: bool = False) -> bool:
    """Can sender DM the target instance? Same owner, public target, or admin sender."""
    if sender_is_admin:
        return True
    if target.get("owner_id") == sender_owner_id:
        return True
    return target.get("visibility") == "public"

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class SendMessageRequest(BaseModel):
    content: str
    to_id: Optional[str] = None

class AdminMessageRequest(BaseModel):
    content: str
    to_id: Optional[str] = None

class UpdateInstanceRequest(BaseModel):
    name: Optional[str]         = None
    display_name: Optional[str] = None
    notes: Optional[str]        = None
    visibility: Optional[str]   = None  # 'private' | 'public', owner-settable

class CreateUserRequest(BaseModel):
    username: str
    password: str
    is_admin: bool = False

class CreateApiKeyRequest(BaseModel):
    label: str = ""

# ---------------------------------------------------------------------------
# Setup / Bootstrap
# ---------------------------------------------------------------------------

@app.get("/setup", response_class=HTMLResponse)
async def setup_page(token: str = ""):
    global _bootstrap_token, _bootstrap_expiry
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if count > 0:
        return HTMLResponse("<h2>Setup already complete. <a href='/'>Go to login</a></h2>")
    if not _bootstrap_token or token != _bootstrap_token or time.time() > _bootstrap_expiry:
        return HTMLResponse("<h2>Invalid or expired setup token. Restart the server to get a new one.</h2>", status_code=403)
    return HTMLResponse(_SETUP_HTML.replace("__TOKEN__", token))


@app.post("/setup")
async def setup_submit(
    token: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
):
    global _bootstrap_token, _bootstrap_expiry
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if count > 0:
        raise HTTPException(400, "Setup already complete")
    if not _bootstrap_token or token != _bootstrap_token or time.time() > _bootstrap_expiry:
        raise HTTPException(403, "Invalid or expired setup token")
    if len(password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")

    user_id = uuid.uuid4().hex
    pw_hash = _hash_password(password)
    with db() as conn:
        conn.execute(
            "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (?,?,?,1,?)",
            (user_id, username.strip(), pw_hash, time.time()),
        )

    _bootstrap_token  = None  # invalidate immediately
    _bootstrap_expiry = 0.0
    print(f"[AI Mesh] Admin account created: {username}")

    response = RedirectResponse("/", status_code=303)
    session_token = _make_jwt({"sub": user_id, "is_admin": True})
    response.set_cookie("mesh_session", session_token, httponly=True, samesite="strict")
    return response


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse(_LOGIN_HTML)


@app.post("/auth/login")
async def auth_login(username: str = Form(...), password: str = Form(...)):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username.strip(),)
        ).fetchone()

    if not row or not _verify_password(password, row["password_hash"]):
        return HTMLResponse(
            _LOGIN_HTML.replace("__ERROR__", "Invalid username or password"),
            status_code=401,
        )

    token = _make_jwt({"sub": row["id"], "is_admin": bool(row["is_admin"])})
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("mesh_session", token, httponly=True, samesite="strict")
    return response


@app.post("/auth/logout")
async def auth_logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("mesh_session")
    return response


@app.get("/api/me")
async def api_me(mesh_session: Optional[str] = Cookie(default=None)):
    user = _session_user(mesh_session)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return {
        "id": user["id"],
        "username": user["username"],
        "is_admin": bool(user["is_admin"]),
    }


@app.get("/api/ws-token")
async def api_ws_token(mesh_session: Optional[str] = Cookie(default=None)):
    """Issue a short-lived token for WebSocket auth (cookies unavailable in WS handshake)."""
    user = _session_user(mesh_session)
    if not user:
        raise HTTPException(401, "Not authenticated")
    token = _make_jwt(
        {"sub": user["id"], "is_admin": bool(user["is_admin"]), "ws": True},
        expire_minutes=WS_TOKEN_EXPIRE_MINUTES,
    )
    return {"token": token}


# ---------------------------------------------------------------------------
# User management (admin only)
# ---------------------------------------------------------------------------

@app.post("/api/admin/users")
async def create_user(req: CreateUserRequest, _admin=..., mesh_session: Optional[str] = Cookie(default=None)):
    user = _session_user(mesh_session)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin required")
    if len(req.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    with db() as conn:
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (req.username,)).fetchone()
        if existing:
            raise HTTPException(409, "Username already exists")
        uid = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (?,?,?,?,?)",
            (uid, req.username.strip(), _hash_password(req.password), int(req.is_admin), time.time()),
        )
    return {"user_id": uid, "username": req.username}


@app.get("/api/admin/users")
async def list_users(mesh_session: Optional[str] = Cookie(default=None)):
    user = _session_user(mesh_session)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin required")
    with db() as conn:
        rows = conn.execute(
            "SELECT id, username, is_admin, created_at FROM users ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


@app.delete("/api/admin/users/{user_id}")
async def delete_user(user_id: str, mesh_session: Optional[str] = Cookie(default=None)):
    user = _session_user(mesh_session)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin required")
    if user_id == user["id"]:
        raise HTTPException(400, "Cannot delete your own account")
    with db() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.execute("UPDATE api_keys SET revoked = 1 WHERE owner_id = ?", (user_id,))
    return {"ok": True}


# ---------------------------------------------------------------------------
# API key management
# ---------------------------------------------------------------------------

@app.post("/api/admin/api-keys")
async def create_api_key(req: CreateApiKeyRequest, mesh_session: Optional[str] = Cookie(default=None)):
    user = _session_user(mesh_session)
    if not user:
        raise HTTPException(401, "Login required")

    raw_key   = "mesh_" + secrets.token_hex(20)  # mesh_ + 40 hex = 45 chars
    key_prefix = raw_key[:13]                      # mesh_ + first 8 hex
    key_hash   = _hash_api_key(raw_key)
    key_id     = uuid.uuid4().hex

    with db() as conn:
        conn.execute(
            "INSERT INTO api_keys (id, key_hash, key_prefix, owner_id, label, created_at) VALUES (?,?,?,?,?,?)",
            (key_id, key_hash, key_prefix, user["id"], req.label.strip(), time.time()),
        )

    # Return the raw key ONCE — never stored in plaintext
    return {"key_id": key_id, "key": raw_key, "prefix": key_prefix, "label": req.label}


@app.get("/api/admin/api-keys")
async def list_api_keys(mesh_session: Optional[str] = Cookie(default=None)):
    user = _session_user(mesh_session)
    if not user:
        raise HTTPException(401, "Login required")
    with db() as conn:
        rows = conn.execute(
            "SELECT id, key_prefix, label, created_at, last_used, revoked FROM api_keys WHERE owner_id = ? ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()
    return [dict(r) for r in rows]


@app.delete("/api/admin/api-keys/{key_id}")
async def revoke_api_key(key_id: str, mesh_session: Optional[str] = Cookie(default=None)):
    user = _session_user(mesh_session)
    if not user:
        raise HTTPException(401, "Login required")
    with db() as conn:
        row = conn.execute("SELECT owner_id FROM api_keys WHERE id = ?", (key_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        if row["owner_id"] != user["id"] and not user["is_admin"]:
            raise HTTPException(403)
        conn.execute("UPDATE api_keys SET revoked = 1 WHERE id = ?", (key_id,))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Instance API (requires API key)
# ---------------------------------------------------------------------------

@app.post("/api/register")
async def register(
    request: Request,
    x_api_key: Optional[str] = Header(default=None),
):
    key_row = _resolve_api_key(x_api_key)
    if not key_row:
        raise HTTPException(401, "Valid X-API-Key required")

    body = await request.json()
    name          = body.get("name", "unknown")
    instance_type = body.get("instance_type", "claude-code")
    system_info   = body.get("system_info", {})
    now = time.time()

    instance_id = uuid.uuid4().hex[:8]
    with db() as conn:
        conn.execute(
            """INSERT INTO instances
               (id, name, display_name, instance_type, system_info, notes, owner_id, last_seen, created_at, connected)
               VALUES (?,?,NULL,?,?,'' ,?,?,?,1)""",
            (instance_id, name, instance_type, json.dumps(system_info), key_row["owner_id"], now, now),
        )

    await admins_ws.broadcast({"type": "instance_connected", "id": instance_id})
    return {"instance_id": instance_id}


@app.post("/api/heartbeat")
async def heartbeat(x_api_key: Optional[str] = Header(default=None)):
    key_row = _resolve_api_key(x_api_key)
    if not key_row:
        raise HTTPException(401, "Valid X-API-Key required")
    # Update any instance owned by this key that is connected
    now = time.time()
    with db() as conn:
        conn.execute(
            "UPDATE instances SET last_seen = ?, connected = 1 WHERE owner_id = ?",
            (now, key_row["owner_id"]),
        )
    return {"ok": True}


@app.post("/api/messages")
async def send_message(req: SendMessageRequest, x_api_key: Optional[str] = Header(default=None)):
    key_row = _resolve_api_key(x_api_key)
    if not key_row:
        raise HTTPException(401, "Valid X-API-Key required")

    # Find this key's instance
    with db() as conn:
        inst = conn.execute(
            "SELECT * FROM instances WHERE owner_id = ? AND connected = 1 ORDER BY last_seen DESC",
            (key_row["owner_id"],),
        ).fetchone()
    if not inst:
        raise HTTPException(404, "No connected instance found for this API key")

    # If DMing, verify the sender is allowed to message the target
    if req.to_id:
        with db() as conn:
            target = conn.execute(
                "SELECT id, owner_id, visibility FROM instances WHERE id = ?",
                (req.to_id,),
            ).fetchone()
        if not target:
            raise HTTPException(404, "Target instance not found")
        if not _instance_messageable_by(key_row["owner_id"], dict(target)):
            raise HTTPException(403, "Target instance is private and not owned by you")

    msg_id = uuid.uuid4().hex
    now    = time.time()
    with db() as conn:
        conn.execute(
            "INSERT INTO messages (id, from_id, to_id, content, timestamp, read) VALUES (?,?,?,?,?,0)",
            (msg_id, inst["id"], req.to_id, req.content, now),
        )

    event = {
        "type":      "message",
        "id":        msg_id,
        "from_id":   inst["id"],
        "from_name": inst["display_name"] or inst["name"],
        "to_id":     req.to_id,
        "content":   req.content,
        "timestamp": now,
    }
    if req.to_id:
        await instances_ws.send(req.to_id, event)
    else:
        # Broadcast only to instances the sender can reach: own + public
        with db() as conn:
            recipients = conn.execute(
                """SELECT id FROM instances
                   WHERE id != ? AND (owner_id = ? OR visibility = 'public')""",
                (inst["id"], key_row["owner_id"]),
            ).fetchall()
        for r in recipients:
            await instances_ws.send(r["id"], event)
    await admins_ws.broadcast(event)
    return {"message_id": msg_id}


@app.get("/api/messages")
async def get_messages(limit: int = 50, x_api_key: Optional[str] = Header(default=None)):
    key_row = _resolve_api_key(x_api_key)
    if not key_row:
        raise HTTPException(401, "Valid X-API-Key required")

    with db() as conn:
        inst = conn.execute(
            "SELECT * FROM instances WHERE owner_id = ? ORDER BY last_seen DESC",
            (key_row["owner_id"],),
        ).fetchone()
    if not inst:
        return []

    iid = inst["id"]
    with db() as conn:
        rows = conn.execute(
            """SELECT m.*, i.name as from_name, i.display_name as from_display
               FROM messages m
               JOIN instances i ON m.from_id = i.id
               WHERE m.to_id = ? OR m.to_id IS NULL
               ORDER BY m.timestamp DESC LIMIT ?""",
            (iid, limit),
        ).fetchall()
        conn.execute("UPDATE messages SET read = 1 WHERE to_id = ?", (iid,))
    return [dict(r) for r in rows]


@app.patch("/api/instances/{instance_id}")
async def update_instance(
    instance_id: str,
    req: UpdateInstanceRequest,
    x_api_key: Optional[str] = Header(default=None),
    mesh_session: Optional[str] = Cookie(default=None),
):
    # Allow: instance owner via API key, or admin via session
    key_row  = _resolve_api_key(x_api_key)
    session  = _session_user(mesh_session)

    with db() as conn:
        row = conn.execute("SELECT * FROM instances WHERE id = ?", (instance_id,)).fetchone()
    if not row:
        raise HTTPException(404)

    is_owner = key_row and row["owner_id"] == key_row["owner_id"]
    is_admin = session and session["is_admin"]

    if not is_owner and not is_admin:
        raise HTTPException(403, "Not authorized")

    updates: dict = {}
    if req.display_name is not None and is_admin:
        updates["display_name"] = req.display_name or None
    if req.notes is not None and is_admin:
        updates["notes"] = req.notes
    if req.name is not None and is_owner:
        updates["name"] = req.name
    if req.visibility is not None and (is_owner or is_admin):
        if req.visibility not in ("private", "public"):
            raise HTTPException(400, "visibility must be 'private' or 'public'")
        updates["visibility"] = req.visibility

    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        with db() as conn:
            conn.execute(
                f"UPDATE instances SET {set_clause} WHERE id = ?",
                (*updates.values(), instance_id),
            )

    await admins_ws.broadcast({"type": "instance_updated", "id": instance_id})
    return {"ok": True}


# ---------------------------------------------------------------------------
# Instance WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws/instance")
async def ws_instance(ws: WebSocket, api_key: str = ""):
    key_row = _resolve_api_key(api_key)
    if not key_row:
        await ws.close(code=4001)
        return

    with db() as conn:
        inst = conn.execute(
            "SELECT * FROM instances WHERE owner_id = ? ORDER BY last_seen DESC",
            (key_row["owner_id"],),
        ).fetchone()
    if not inst:
        await ws.close(code=4002)
        return

    iid = inst["id"]
    await instances_ws.connect(iid, ws)
    now = time.time()
    with db() as conn:
        conn.execute(
            "UPDATE instances SET connected = 1, last_seen = ? WHERE id = ?",
            (now, iid),
        )
    await admins_ws.broadcast({"type": "instance_connected", "id": iid})

    try:
        while True:
            data = await ws.receive_json()
            if data.get("type") == "pong":
                with db() as conn:
                    conn.execute(
                        "UPDATE instances SET last_seen = ? WHERE id = ?",
                        (time.time(), iid),
                    )
    except WebSocketDisconnect:
        pass
    finally:
        instances_ws.disconnect(iid)
        with db() as conn:
            conn.execute("UPDATE instances SET connected = 0 WHERE id = ?", (iid,))
        await admins_ws.broadcast({"type": "instance_disconnected", "id": iid})


# ---------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------

@app.get("/api/instances")
async def list_instances(mesh_session: Optional[str] = Cookie(default=None)):
    user = _session_user(mesh_session)
    if not user:
        raise HTTPException(401, "Login required")
    with db() as conn:
        if user["is_admin"]:
            rows = conn.execute(
                """SELECT i.*, u.username AS owner_username
                   FROM instances i LEFT JOIN users u ON i.owner_id = u.id
                   ORDER BY i.last_seen DESC"""
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT i.*, u.username AS owner_username
                   FROM instances i LEFT JOIN users u ON i.owner_id = u.id
                   WHERE i.owner_id = ? OR i.visibility = 'public'
                   ORDER BY i.last_seen DESC""",
                (user["id"],),
            ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/instances/{instance_id}")
async def get_instance(instance_id: str, mesh_session: Optional[str] = Cookie(default=None)):
    user = _session_user(mesh_session)
    if not user:
        raise HTTPException(401, "Login required")
    with db() as conn:
        row = conn.execute(
            """SELECT i.*, u.username AS owner_username
               FROM instances i LEFT JOIN users u ON i.owner_id = u.id
               WHERE i.id = ?""",
            (instance_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404)
        if not _user_can_see_instance(user, dict(row)):
            raise HTTPException(403, "Not authorized to view this instance")
        msgs = conn.execute(
            """SELECT m.*, i.name as from_name, i.display_name as from_display
               FROM messages m
               JOIN instances i ON m.from_id = i.id
               WHERE m.from_id = ? OR m.to_id = ? OR m.to_id IS NULL
               ORDER BY m.timestamp DESC LIMIT 50""",
            (instance_id, instance_id),
        ).fetchall()
    return {"instance": dict(row), "messages": [dict(m) for m in msgs]}


@app.post("/api/admin/message")
async def admin_message(req: AdminMessageRequest, mesh_session: Optional[str] = Cookie(default=None)):
    user = _session_user(mesh_session)
    if not user:
        raise HTTPException(401, "Login required")
    is_admin = bool(user["is_admin"])

    # Permission check: non-admins can only DM instances they can see, or
    # broadcast (which we filter to their visible set below)
    if req.to_id:
        with db() as conn:
            target = conn.execute(
                "SELECT id, owner_id, visibility FROM instances WHERE id = ?",
                (req.to_id,),
            ).fetchone()
        if not target:
            raise HTTPException(404, "Target instance not found")
        if not _instance_messageable_by(user["id"], dict(target), is_admin):
            raise HTTPException(403, "Target instance is private and not owned by you")

    msg_id = uuid.uuid4().hex
    now    = time.time()
    sender = f"🖥 {user['username']}"

    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO instances (id, name, display_name, instance_type, system_info, notes, owner_id, last_seen, created_at, connected) VALUES ('admin','Admin GUI','🖥 Admin','admin','{}','',NULL,?,?,0)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO messages (id, from_id, to_id, content, timestamp, read) VALUES (?,?,?,?,?,1)",
            (msg_id, "admin", req.to_id, req.content, now),
        )

    event = {
        "type":      "message",
        "id":        msg_id,
        "from_id":   "admin",
        "from_name": sender,
        "to_id":     req.to_id,
        "content":   req.content,
        "timestamp": now,
    }
    if req.to_id:
        await instances_ws.send(req.to_id, event)
    elif is_admin:
        await instances_ws.broadcast(event)
    else:
        # Non-admin broadcast: only to instances this user can reach
        with db() as conn:
            recipients = conn.execute(
                """SELECT id FROM instances
                   WHERE id != 'admin' AND (owner_id = ? OR visibility = 'public')""",
                (user["id"],),
            ).fetchall()
        for r in recipients:
            await instances_ws.send(r["id"], event)
    await admins_ws.broadcast(event)
    return {"message_id": msg_id}


# ---------------------------------------------------------------------------
# Admin WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws/admin")
async def ws_admin(ws: WebSocket, token: str = ""):
    try:
        payload = _decode_jwt(token)
        if not payload.get("ws"):
            await ws.close(code=4001)
            return
    except JWTError:
        await ws.close(code=4001)
        return

    await admins_ws.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        admins_ws.disconnect(ws)


# ---------------------------------------------------------------------------
# Static / download
# ---------------------------------------------------------------------------

@app.get("/download/mcp-client")
async def download_client(mesh_session: Optional[str] = Cookie(default=None)):
    user = _session_user(mesh_session)
    if not user:
        raise HTTPException(401, "Login required")
    if not CLIENT_PATH.exists():
        raise HTTPException(404, "Client file not found on server")
    return FileResponse(CLIENT_PATH, filename="mcp_client.py", media_type="text/x-python")


# ---------------------------------------------------------------------------
# Web GUI SPA
# ---------------------------------------------------------------------------

@app.get("/instances/{instance_id}", response_class=HTMLResponse)
@app.get("/", response_class=HTMLResponse)
async def gui(request: Request):
    return HTMLResponse(content=_GUI_HTML)


# ---------------------------------------------------------------------------
# Embedded HTML
# ---------------------------------------------------------------------------

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI Mesh — Login</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#e6edf3;min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:40px;width:360px}
h1{font-size:22px;font-weight:700;color:#58a6ff;margin-bottom:6px;text-align:center}
.sub{color:#8b949e;font-size:13px;text-align:center;margin-bottom:28px}
label{display:block;font-size:12px;color:#8b949e;margin-bottom:6px}
input{width:100%;background:#0d1117;border:1px solid #30363d;color:#e6edf3;border-radius:6px;padding:9px 12px;font-size:14px;margin-bottom:16px}
input:focus{outline:none;border-color:#58a6ff}
button{width:100%;background:#1f6feb;color:#fff;border:none;padding:10px;border-radius:6px;font-size:14px;cursor:pointer;font-weight:600}
button:hover{background:#388bfd}
.err{background:#3d1a1a;border:1px solid #f8514933;color:#f85149;padding:10px 12px;border-radius:6px;font-size:13px;margin-bottom:16px}
</style>
</head>
<body>
<div class="card">
  <h1>🤖 AI Mesh</h1>
  <div class="sub">Coordination Server</div>
  __ERROR_BLOCK__
  <form method="post" action="/auth/login">
    <label>Username</label>
    <input name="username" type="text" autocomplete="username" required>
    <label>Password</label>
    <input name="password" type="password" autocomplete="current-password" required>
    <button type="submit">Sign In</button>
  </form>
</div>
</body>
</html>
""".replace("__ERROR_BLOCK__", "").replace(
    "__ERROR__", ""
)

# Patch in error display support
_LOGIN_HTML = _LOGIN_HTML.replace(
    "__ERROR_BLOCK__",
    '<div class="err" id="err" style="display:none"></div>',
)

# Re-export with a simple placeholder approach
_LOGIN_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI Mesh — Login</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#e6edf3;min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:40px;width:360px}
h1{font-size:22px;font-weight:700;color:#58a6ff;margin-bottom:6px;text-align:center}
.sub{color:#8b949e;font-size:13px;text-align:center;margin-bottom:28px}
label{display:block;font-size:12px;color:#8b949e;margin-bottom:6px}
input{width:100%;background:#0d1117;border:1px solid #30363d;color:#e6edf3;border-radius:6px;padding:9px 12px;font-size:14px;margin-bottom:16px}
input:focus{outline:none;border-color:#58a6ff}
button{width:100%;background:#1f6feb;color:#fff;border:none;padding:10px;border-radius:6px;font-size:14px;cursor:pointer;font-weight:600}
button:hover{background:#388bfd}
.err{background:#3d1a1a;border:1px solid #f8514933;color:#f85149;padding:10px 12px;border-radius:6px;font-size:13px;margin-bottom:16px;display:__ERR_DISPLAY__}
</style>
</head>
<body>
<div class="card">
  <h1>🤖 AI Mesh</h1>
  <div class="sub">Coordination Server</div>
  <div class="err">__ERROR__</div>
  <form method="post" action="/auth/login">
    <label>Username</label>
    <input name="username" type="text" autocomplete="username" required>
    <label>Password</label>
    <input name="password" type="password" autocomplete="current-password" required>
    <button type="submit">Sign In</button>
  </form>
</div>
</body>
</html>"""

def _LOGIN_HTML_render(error: str = "") -> str:
    return _LOGIN_HTML_TEMPLATE.replace(
        "__ERR_DISPLAY__", "block" if error else "none"
    ).replace("__ERROR__", error)

# Override the simple version with the rendered one
_LOGIN_HTML = _LOGIN_HTML_render()

# Patch auth_login to use renderer
@app.post("/auth/login", include_in_schema=False)
async def auth_login_override(username: str = Form(...), password: str = Form(...)):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username.strip(),)
        ).fetchone()
    if not row or not _verify_password(password, row["password_hash"]):
        return HTMLResponse(_LOGIN_HTML_render("Invalid username or password"), status_code=401)
    token = _make_jwt({"sub": row["id"], "is_admin": bool(row["is_admin"])})
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("mesh_session", token, httponly=True, samesite="strict")
    return response


_SETUP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>AI Mesh — First Run Setup</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#e6edf3;min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:40px;width:380px}
h1{font-size:20px;font-weight:700;color:#58a6ff;margin-bottom:6px}
.sub{color:#8b949e;font-size:13px;margin-bottom:24px}
label{display:block;font-size:12px;color:#8b949e;margin-bottom:6px}
input{width:100%;background:#0d1117;border:1px solid #30363d;color:#e6edf3;border-radius:6px;padding:9px 12px;font-size:14px;margin-bottom:16px}
input:focus{outline:none;border-color:#58a6ff}
button{width:100%;background:#238636;color:#fff;border:none;padding:10px;border-radius:6px;font-size:14px;cursor:pointer;font-weight:600}
button:hover{background:#2ea043}
</style>
</head>
<body>
<div class="card">
  <h1>🤖 AI Mesh Setup</h1>
  <div class="sub">Create your admin account to get started.</div>
  <form method="post" action="/setup">
    <input type="hidden" name="token" value="__TOKEN__">
    <label>Admin Username</label>
    <input name="username" type="text" required>
    <label>Password (min 8 chars)</label>
    <input name="password" type="password" required minlength="8">
    <button type="submit">Create Admin Account</button>
  </form>
</div>
</body>
</html>"""


_GUI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI Mesh</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#e6edf3;height:100vh;display:flex;flex-direction:column;overflow:hidden}

header{background:#161b22;border-bottom:1px solid #30363d;padding:10px 20px;display:flex;align-items:center;gap:14px;flex-shrink:0}
header h1{font-size:17px;font-weight:700;color:#58a6ff;letter-spacing:-.3px}
header .sub{color:#8b949e;font-size:12px}
.header-right{margin-left:auto;display:flex;align-items:center;gap:10px}
.user-badge{color:#8b949e;font-size:12px}
.dl-btn,
.logout-btn{color:#fff;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px;text-decoration:none;white-space:nowrap}
.dl-btn{background:#238636}.dl-btn:hover{background:#2ea043}
.logout-btn{background:#21262d}.logout-btn:hover{background:#30363d}

.layout{display:flex;flex:1;overflow:hidden}

.sidebar{width:260px;background:#161b22;border-right:1px solid #30363d;display:flex;flex-direction:column;flex-shrink:0}
.sidebar-head{padding:12px 14px 8px;font-size:11px;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid #21262d;display:flex;align-items:center;justify-content:space-between}
.instance-list{flex:1;overflow-y:auto}
.instance-item{padding:10px 14px;border-bottom:1px solid #21262d;cursor:pointer;transition:background .12s}
.instance-item:hover{background:#1c2128}
.instance-item.active{background:#1f2937;border-left:3px solid #58a6ff}
.iname{font-size:13px;font-weight:500;display:flex;align-items:center;gap:7px}
.itype{font-size:11px;color:#8b949e;margin-top:2px}
.ihost{font-size:10px;color:#6e7681;margin-top:1px}
.dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.dot.on{background:#3fb950;box-shadow:0 0 5px #3fb95080}
.dot.off{background:#484f58}
.bc-btn{margin:10px 12px;background:#1a2433;border:1px solid #30363d;color:#58a6ff;padding:7px;border-radius:6px;cursor:pointer;font-size:12px;width:calc(100% - 24px);text-align:center}
.bc-btn:hover{background:#1f2d40}

.panel{flex:1;display:flex;flex-direction:column;overflow:hidden;background:#0d1117}
.panel-header{padding:14px 20px;background:#161b22;border-bottom:1px solid #30363d;flex-shrink:0}
.panel-header h2{font-size:15px;font-weight:600}
.panel-header .meta{font-size:11px;color:#8b949e;margin-top:3px}

.detail-section{padding:14px 20px;border-bottom:1px solid #21262d;display:grid;grid-template-columns:1fr 1fr;gap:12px;flex-shrink:0;overflow-y:auto;max-height:320px}
.detail-item label{font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:.06em;display:block;margin-bottom:5px}
.detail-item .val{font-size:12px;color:#e6edf3}
.edit-row{display:flex;gap:6px;align-items:center}
input.field,textarea.field{background:#0d1117;border:1px solid #30363d;color:#e6edf3;border-radius:5px;padding:5px 9px;font-size:12px;font-family:inherit}
input.field{flex:1}textarea.field{width:100%;resize:vertical;min-height:54px}
.save-btn{background:#1f6feb;color:#fff;border:none;padding:5px 10px;border-radius:5px;cursor:pointer;font-size:11px;white-space:nowrap}
.save-btn:hover{background:#388bfd}
.danger-btn{background:#b91c1c;color:#fff;border:none;padding:4px 8px;border-radius:4px;cursor:pointer;font-size:11px}
.danger-btn:hover{background:#dc2626}
.sysinfo{font-size:11px;color:#8b949e;font-family:'Consolas',monospace;white-space:pre;background:#010409;padding:8px 10px;border-radius:5px;border:1px solid #21262d;overflow-x:auto;max-height:100px}

/* API Keys panel */
.key-row{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid #21262d;font-size:12px}
.key-prefix{font-family:'Consolas',monospace;color:#58a6ff}
.key-label{color:#8b949e;flex:1}
.key-meta{color:#6e7681;font-size:11px}

.messages{flex:1;overflow-y:auto;padding:14px 20px;display:flex;flex-direction:column;gap:8px}
.msg-wrap{display:flex;flex-direction:column}
.msg-wrap.out{align-items:flex-end}.msg-wrap.in{align-items:flex-start}.msg-wrap.bc{align-items:flex-start}
.msg-from{font-size:10px;color:#8b949e;margin-bottom:3px;padding:0 2px}
.bubble{max-width:68%;padding:9px 13px;border-radius:12px;font-size:13px;line-height:1.5;word-break:break-word}
.bubble.out{background:#1f6feb;border-bottom-right-radius:3px}
.bubble.in{background:#21262d;border-bottom-left-radius:3px}
.bubble.bc{background:#0d2918;border:1px solid #2ea04380;border-bottom-left-radius:3px}
.msg-time{font-size:10px;color:#6e7681;margin-top:3px;padding:0 2px}
.empty-msgs{color:#8b949e;text-align:center;padding:40px 0;font-size:13px}

.send-bar{padding:10px 16px;background:#161b22;border-top:1px solid #30363d;display:flex;gap:8px;flex-shrink:0}
.send-bar input{flex:1;background:#0d1117;border:1px solid #30363d;color:#e6edf3;border-radius:7px;padding:9px 13px;font-size:13px}
.send-bar input:focus{outline:none;border-color:#388bfd}
.send-bar button{background:#1f6feb;color:#fff;border:none;padding:9px 16px;border-radius:7px;cursor:pointer;font-size:13px}
.send-bar button:hover{background:#388bfd}

.welcome{flex:1;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:10px;color:#8b949e}
.welcome h3{font-size:18px;color:#e6edf3;font-weight:600}

::-webkit-scrollbar{width:5px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:#30363d;border-radius:3px}
.status-bar{background:#010409;border-top:1px solid #21262d;padding:3px 14px;font-size:10px;color:#6e7681;flex-shrink:0}

/* Admin panel */
.admin-panel{background:#161b22;border-top:1px solid #30363d;padding:12px 20px;flex-shrink:0}
.admin-panel h3{font-size:11px;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px}
.admin-row{display:flex;gap:8px;margin-bottom:6px;align-items:center}
.admin-row input{background:#0d1117;border:1px solid #30363d;color:#e6edf3;border-radius:5px;padding:5px 9px;font-size:12px;flex:1}

/* Owner + visibility badges on sidebar */
.iown{font-size:10px;color:#6e7681;margin-top:2px;display:flex;align-items:center;gap:6px}
.vis-badge{display:inline-block;padding:1px 6px;border-radius:3px;font-size:9px;font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.vis-public{background:#0d2918;color:#3fb950;border:1px solid #2ea04380}
.vis-private{background:#1f0d0d;color:#f85149;border:1px solid #b91c1c80}

/* Modal */
.modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;z-index:100}
.modal{background:#161b22;border:1px solid #30363d;border-radius:8px;width:520px;max-width:92vw;max-height:80vh;overflow:auto;display:flex;flex-direction:column}
.modal-head{padding:14px 18px;border-bottom:1px solid #21262d;display:flex;align-items:center;justify-content:space-between}
.modal-head h2{font-size:15px;font-weight:600;color:#58a6ff}
.modal-close{background:transparent;border:none;color:#8b949e;font-size:20px;cursor:pointer;line-height:1}
.modal-body{padding:14px 18px}
.user-row{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid #21262d;font-size:13px}
.user-row .uname{flex:1;font-weight:500}
.user-row .uflag{font-size:10px;padding:1px 6px;border-radius:3px;background:#0d2918;color:#3fb950;border:1px solid #2ea04380;text-transform:uppercase;letter-spacing:.04em}
.user-form{display:grid;grid-template-columns:1fr 1fr auto auto;gap:6px;margin-top:14px;align-items:center}
.user-form input{background:#0d1117;border:1px solid #30363d;color:#e6edf3;border-radius:5px;padding:6px 9px;font-size:12px}
.user-form label{font-size:11px;color:#8b949e;display:flex;align-items:center;gap:5px}

/* Header user link */
.users-btn{color:#fff;background:#21262d;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px}
.users-btn:hover{background:#30363d}
</style>
</head>
<body>
<header>
  <h1>🤖 AI Mesh</h1>
  <span class="sub">Coordination Server</span>
  <div class="header-right">
    <span class="user-badge" id="userBadge"></span>
    <button id="usersBtn" class="users-btn" style="display:none" onclick="openUsersModal()">👥 Users</button>
    <a href="/download/mcp-client" class="dl-btn">⬇ MCP Client</a>
    <form method="post" action="/auth/logout" style="display:inline">
      <button class="logout-btn" type="submit">Sign out</button>
    </form>
  </div>
</header>

<div class="layout">
  <div class="sidebar">
    <div class="sidebar-head">
      <span>Instances</span>
      <span id="countBadge" style="color:#3fb950;font-weight:700"></span>
    </div>
    <button class="bc-btn" onclick="selectBroadcast()">📢 Broadcast to All</button>
    <div class="instance-list" id="instanceList"></div>
  </div>
  <div class="panel" id="panel">
    <div class="welcome">
      <h3>AI Mesh Coordination Server</h3>
      <p>Select an instance from the sidebar or broadcast to all.</p>
    </div>
  </div>
</div>
<div class="status-bar" id="statusBar">Connecting…</div>

<script>
const S = { instances:{}, selected:null, me:null, wsToken:null };

// ── Init ──────────────────────────────────────────────────────────────────────
async function init() {
  const me = await fetch('/api/me').then(r => r.ok ? r.json() : null);
  if (!me) { location.href = '/login'; return; }
  S.me = me;
  document.getElementById('userBadge').textContent = `👤 ${me.username}${me.is_admin ? ' (admin)' : ''}`;
  if (me.is_admin) document.getElementById('usersBtn').style.display = 'inline-block';

  const wt = await fetch('/api/ws-token').then(r => r.json());
  S.wsToken = wt.token;

  connectWS();
  loadInstances();
}

// ── WebSocket ──────────────────────────────────────────────────────────────────
let ws;
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws/admin?token=${S.wsToken}`);
  ws.onopen = () => { document.getElementById('statusBar').textContent = 'Connected'; };
  ws.onclose = () => {
    document.getElementById('statusBar').textContent = 'Disconnected — reconnecting…';
    setTimeout(connectWS, 3000);
  };
  ws.onmessage = e => {
    const ev = JSON.parse(e.data);
    if (['instance_connected','instance_disconnected','instance_updated'].includes(ev.type)) {
      loadInstances();
    }
    if (ev.type === 'message') {
      loadInstances();
      if (S.selected === ev.from_id || S.selected === ev.to_id || (!ev.to_id && S.selected !== null)) {
        loadDetail(S.selected);
      }
      if (S.selected === null && !ev.to_id) appendBroadcastMsg(ev);
    }
  };
}

// ── Instances ──────────────────────────────────────────────────────────────────
async function loadInstances() {
  const r = await fetch('/api/instances');
  if (r.status === 401) { location.href = '/login'; return; }
  const list = await r.json();
  S.instances = {};
  list.forEach(i => { if (i.id !== 'admin') S.instances[i.id] = i; });
  renderSidebar(Object.values(S.instances));
}

function renderSidebar(list) {
  const online = list.filter(i => i.connected).length;
  document.getElementById('countBadge').textContent = `${online}/${list.length}`;
  const el = document.getElementById('instanceList');
  el.innerHTML = list.map(i => {
    const name = esc(i.display_name || i.name);
    const sys  = safeJson(i.system_info);
    const vis  = i.visibility === 'public' ? 'public' : 'private';
    const owner = i.owner_username ? `owned by ${esc(i.owner_username)}` : '';
    return `<div class="instance-item${S.selected===i.id?' active':''}" onclick="selectInstance('${i.id}')">
      <div class="iname"><span class="dot ${i.connected?'on':'off'}"></span>${name}</div>
      <div class="itype">${esc(i.instance_type)} · ${i.id}</div>
      <div class="iown">
        <span class="vis-badge vis-${vis}">${vis === 'public' ? '🌐 public' : '🔒 private'}</span>
        <span>${owner}</span>
      </div>
      <div class="ihost">${esc(sys.hostname||'')}</div>
    </div>`;
  }).join('');
}

async function selectInstance(id) {
  S.selected = id;
  renderSidebar(Object.values(S.instances));
  await loadDetail(id);
  history.replaceState({}, '', `/instances/${id}`);
}

function selectBroadcast() {
  S.selected = null;
  renderSidebar(Object.values(S.instances));
  history.replaceState({}, '', '/');
  document.getElementById('panel').innerHTML = `
    <div class="panel-header"><h2>📢 Broadcast to All</h2><div class="meta">Sent to every connected instance</div></div>
    <div class="messages" id="msgList"></div>
    <div class="send-bar">
      <input id="msgInput" placeholder="Broadcast a message…" onkeydown="if(event.key==='Enter')sendMsg()">
      <button onclick="sendMsg()">Send</button>
    </div>`;
}

async function loadDetail(id) {
  const r = await fetch(`/api/instances/${id}`);
  if (!r.ok) return;
  const data = await r.json();
  const inst = data.instance;
  const sys  = safeJson(inst.system_info);
  const displayName = esc(inst.display_name || inst.name);
  const isAdmin = S.me && S.me.is_admin;
  const isOwner = S.me && inst.owner_id === S.me.id;

  document.getElementById('panel').innerHTML = `
    <div class="panel-header">
      <h2>${displayName} <span style="color:#8b949e;font-size:12px;font-weight:400">#${inst.id}</span></h2>
      <div class="meta">${esc(inst.instance_type)} · Last seen: ${inst.last_seen ? new Date(inst.last_seen*1000).toLocaleString() : 'never'}</div>
    </div>
    <div class="detail-section">
      ${isAdmin ? `
      <div class="detail-item">
        <label>Override Display Name</label>
        <div class="edit-row">
          <input class="field" id="ovName" value="${esc(inst.display_name||'')}" placeholder="Admin display name…">
          <button class="save-btn" onclick="saveOverride('${inst.id}')">Save</button>
        </div>
      </div>` : ''}
      <div class="detail-item">
        <label>Self-Set Name</label>
        <div class="val">${esc(inst.name)}</div>
      </div>
      <div class="detail-item">
        <label>Owner</label>
        <div class="val">${esc(inst.owner_username || '(none)')}</div>
      </div>
      <div class="detail-item">
        <label>Visibility</label>
        ${(isOwner || isAdmin) ? `
          <div class="edit-row">
            <select class="field" id="visSelect">
              <option value="private" ${inst.visibility !== 'public' ? 'selected' : ''}>🔒 Private (only you)</option>
              <option value="public"  ${inst.visibility === 'public' ? 'selected' : ''}>🌐 Public (any user can DM)</option>
            </select>
            <button class="save-btn" onclick="saveVisibility('${inst.id}')">Save</button>
          </div>` : `
          <div class="val"><span class="vis-badge vis-${inst.visibility === 'public' ? 'public' : 'private'}">${inst.visibility === 'public' ? '🌐 public' : '🔒 private'}</span></div>
        `}
      </div>
      ${isAdmin ? `
      <div class="detail-item" style="grid-column:1/-1">
        <label>Admin Notes</label>
        <textarea class="field" id="notesArea" placeholder="Notes…">${esc(inst.notes||'')}</textarea>
        <button class="save-btn" style="margin-top:6px" onclick="saveNotes('${inst.id}')">Save Notes</button>
      </div>` : ''}
      <div class="detail-item" style="grid-column:1/-1">
        <label>System Info</label>
        <div class="sysinfo">${esc(JSON.stringify(sys,null,2))}</div>
      </div>
      ${isAdmin ? `
      <div class="detail-item" style="grid-column:1/-1">
        <label>API Keys (owner)</label>
        <div id="keyList"><em style="color:#6e7681;font-size:12px">Loading…</em></div>
        <div class="admin-row" style="margin-top:8px">
          <input id="keyLabel" class="field" placeholder="Key label (e.g. claude-desktop)">
          <button class="save-btn" onclick="genKey()">Generate Key</button>
        </div>
        <div id="newKey" style="display:none;margin-top:8px;background:#010409;border:1px solid #3fb950;padding:8px;border-radius:5px;font-family:Consolas;font-size:12px;color:#3fb950;word-break:break-all"></div>
      </div>` : ''}
    </div>
    <div class="messages" id="msgList">${renderMessages(data.messages, id)}</div>
    <div class="send-bar">
      <input id="msgInput" placeholder="Message ${displayName}…" onkeydown="if(event.key==='Enter')sendMsg()">
      <button onclick="sendMsg()">Send</button>
    </div>`;

  const ml = document.getElementById('msgList');
  if (ml) ml.scrollTop = ml.scrollHeight;

  if (isAdmin) loadKeys();
}

// ── API Keys ───────────────────────────────────────────────────────────────────
async function loadKeys() {
  const kl = document.getElementById('keyList');
  if (!kl) return;
  const r = await fetch('/api/admin/api-keys');
  const keys = await r.json();
  if (!keys.length) { kl.innerHTML = '<em style="color:#6e7681;font-size:12px">No keys yet</em>'; return; }
  kl.innerHTML = keys.map(k => `
    <div class="key-row">
      <span class="key-prefix">${esc(k.key_prefix)}…</span>
      <span class="key-label">${esc(k.label||'(no label)')}</span>
      <span class="key-meta">${k.last_used ? 'Used '+new Date(k.last_used*1000).toLocaleDateString() : 'Never used'}</span>
      ${k.revoked ? '<span style="color:#f85149;font-size:11px">Revoked</span>' :
        `<button class="danger-btn" onclick="revokeKey('${k.id}')">Revoke</button>`}
    </div>`).join('');
}

async function genKey() {
  const label = document.getElementById('keyLabel')?.value || '';
  const r = await fetch('/api/admin/api-keys', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({label})
  });
  const data = await r.json();
  const nk = document.getElementById('newKey');
  if (nk) {
    nk.style.display = 'block';
    nk.textContent = `⚠ Copy now — shown once:\n${data.key}`;
  }
  loadKeys();
}

async function revokeKey(id) {
  await fetch(`/api/admin/api-keys/${id}`, {method:'DELETE'});
  loadKeys();
}

// ── Messages ───────────────────────────────────────────────────────────────────
function renderMessages(messages, selectedId) {
  if (!messages.length) return '<div class="empty-msgs">No messages yet</div>';
  return messages.slice().reverse().map(m => {
    const isBc = !m.to_id;
    const fromInst = S.instances[m.from_id];
    const fromName = fromInst ? (fromInst.display_name||fromInst.name) : (m.from_display||m.from_name||m.from_id);
    const cls = isBc ? 'bc' : (m.from_id === selectedId ? 'in' : 'out');
    const label = isBc ? `📢 ${esc(fromName)} (broadcast)` : esc(fromName);
    return `<div class="msg-wrap ${cls}">
      <div class="msg-from">${label}</div>
      <div class="bubble ${cls}">${esc(m.content).replace(/\\n/g,'<br>')}</div>
      <div class="msg-time">${new Date(m.timestamp*1000).toLocaleTimeString()}</div>
    </div>`;
  }).join('');
}

function appendBroadcastMsg(ev) {
  const ml = document.getElementById('msgList');
  if (!ml) return;
  const fromInst = S.instances[ev.from_id];
  const fromName = fromInst ? (fromInst.display_name||fromInst.name) : ev.from_name;
  ml.innerHTML += `<div class="msg-wrap bc">
    <div class="msg-from">📢 ${esc(fromName)} (broadcast)</div>
    <div class="bubble bc">${esc(ev.content).replace(/\\n/g,'<br>')}</div>
    <div class="msg-time">${new Date(ev.timestamp*1000).toLocaleTimeString()}</div>
  </div>`;
  ml.scrollTop = ml.scrollHeight;
}

// ── Send ───────────────────────────────────────────────────────────────────────
async function sendMsg() {
  const inp = document.getElementById('msgInput');
  const content = inp.value.trim();
  if (!content) return;
  inp.value = '';
  await fetch('/api/admin/message', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({content, to_id: S.selected || null})
  });
  if (S.selected) await loadDetail(S.selected);
}

async function saveOverride(id) {
  const val = document.getElementById('ovName')?.value.trim();
  await fetch(`/api/instances/${id}`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({display_name:val||null})});
}

async function saveNotes(id) {
  const val = document.getElementById('notesArea')?.value;
  await fetch(`/api/instances/${id}`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({notes:val})});
}

async function saveVisibility(id) {
  const val = document.getElementById('visSelect')?.value;
  const r = await fetch(`/api/instances/${id}`, {
    method:'PATCH', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({visibility:val})
  });
  if (r.ok) { loadInstances(); loadDetail(id); }
  else alert('Failed to update visibility');
}

// ── Users modal (admin only) ───────────────────────────────────────────────────
function openUsersModal() {
  const modal = document.createElement('div');
  modal.className = 'modal-backdrop';
  modal.id = 'usersModal';
  modal.innerHTML = `
    <div class="modal">
      <div class="modal-head">
        <h2>👥 Users</h2>
        <button class="modal-close" onclick="closeUsersModal()">×</button>
      </div>
      <div class="modal-body">
        <div id="userList"><em style="color:#6e7681">Loading…</em></div>
        <div class="user-form">
          <input id="newUsername" placeholder="username">
          <input id="newPassword" type="password" placeholder="password (min 8)">
          <label><input type="checkbox" id="newIsAdmin"> admin</label>
          <button class="save-btn" onclick="createUser()">Add User</button>
        </div>
        <div id="userFormMsg" style="margin-top:8px;font-size:12px"></div>
      </div>
    </div>`;
  modal.onclick = e => { if (e.target === modal) closeUsersModal(); };
  document.body.appendChild(modal);
  loadUsers();
}

function closeUsersModal() {
  document.getElementById('usersModal')?.remove();
}

async function loadUsers() {
  const r = await fetch('/api/admin/users');
  if (!r.ok) {
    document.getElementById('userList').innerHTML = '<em style="color:#f85149">Failed to load users</em>';
    return;
  }
  const users = await r.json();
  const el = document.getElementById('userList');
  el.innerHTML = users.map(u => `
    <div class="user-row">
      <span class="uname">${esc(u.username)}</span>
      ${u.is_admin ? '<span class="uflag">admin</span>' : ''}
      <span style="color:#6e7681;font-size:11px">${new Date(u.created_at*1000).toLocaleDateString()}</span>
      ${u.id === S.me.id ? '<span style="color:#8b949e;font-size:11px">(you)</span>' :
        `<button class="danger-btn" onclick="deleteUser('${u.id}','${esc(u.username)}')">Delete</button>`}
    </div>`).join('');
}

async function createUser() {
  const username = document.getElementById('newUsername').value.trim();
  const password = document.getElementById('newPassword').value;
  const is_admin = document.getElementById('newIsAdmin').checked;
  const msg = document.getElementById('userFormMsg');
  if (!username || password.length < 8) {
    msg.textContent = 'Username required, password ≥ 8 chars';
    msg.style.color = '#f85149';
    return;
  }
  const r = await fetch('/api/admin/users', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({username, password, is_admin})
  });
  if (r.ok) {
    msg.textContent = `User '${username}' created.`;
    msg.style.color = '#3fb950';
    document.getElementById('newUsername').value = '';
    document.getElementById('newPassword').value = '';
    document.getElementById('newIsAdmin').checked = false;
    loadUsers();
  } else {
    const err = await r.json().catch(() => ({detail:'failed'}));
    msg.textContent = formatErr(err);
    msg.style.color = '#f85149';
  }
}

function formatErr(err) {
  // FastAPI returns detail as either a string (HTTPException) or an array
  // of {loc, msg, type} dicts (validation errors). Render both legibly.
  const d = err && err.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) {
    return d.map(e => {
      const field = Array.isArray(e.loc) ? e.loc.filter(x => x !== 'body').join('.') : '';
      return field ? `${field}: ${e.msg}` : e.msg;
    }).join('; ');
  }
  return 'Request failed';
}

async function deleteUser(id, username) {
  if (!confirm(`Delete user '${username}'? Their API keys will be revoked.`)) return;
  const r = await fetch(`/api/admin/users/${id}`, {method:'DELETE'});
  if (r.ok) loadUsers();
  else alert('Failed to delete user');
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function safeJson(s){ try{return JSON.parse(s||'{}')}catch{return{}} }

init();
const pathMatch = location.pathname.match(/^\\/instances\\/([\\w-]+)$/);
if (pathMatch) selectInstance(pathMatch[1]);
</script>
</body>
</html>
"""
