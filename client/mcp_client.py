"""
AI Mesh MCP Client
Connects a Claude Code (or any MCP-capable) instance to the AI Mesh server.

Usage:
  python mcp_client.py                         # stdio MCP server (default)
  AI_MESH_URL=http://host:8000 python mcp_client.py

Add to Claude Code (.claude/mcp_servers.json):
  {
    "ai-mesh": {
      "command": "python",
      "args": ["/path/to/mcp_client.py"],
      "env": { "AI_MESH_URL": "http://your-server:8000" }
    }
  }
"""

import asyncio
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx
import websockets
from fastmcp import FastMCP, Context

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SERVER_URL: str = os.environ.get("AI_MESH_URL", "http://localhost:8000").rstrip("/")
CONFIG_FILE: Path = Path.home() / ".ai-mesh" / "config.json"
INCOMING_FILE: Path = Path.home() / ".ai-mesh" / "incoming.json"

# Env vars various MCP hosts expose. Per-session IDs are checked first so
# parallel sessions of the same host auto-distinguish. Project-dir env vars
# come next so different repos under the same host get different identities.
# Add new tools here as they're discovered.
_SESSION_ID_VARS = (
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",   # Claude Code (CLI / Desktop)
    "CODEX_SESSION_ID", "OPENAI_CODEX_SESSION_ID",   # OpenAI Codex CLI
    "CURSOR_SESSION_ID",                              # Cursor
    "CONTINUE_SESSION_ID",                            # Continue.dev
    "AIDER_SESSION_ID",                               # Aider
)
_PROJECT_DIR_VARS = (
    "CLAUDE_PROJECT_DIR", "CLAUDE_WORKING_DIR",
    "CODEX_PROJECT_DIR",
    "CURSOR_WORKSPACE",
    "INIT_CWD", "PWD",
)


def _detect_host_tool() -> str:
    """Best-effort guess at which MCP host spawned us, for display in the GUI."""
    e = os.environ
    if any(k for k in e if k.startswith("CLAUDE_CODE_")): return "claude-code"
    if any(k for k in e if k.startswith("CLAUDE_")):      return "claude"
    if any(k for k in e if k.startswith("CODEX_")):       return "codex"
    if any(k for k in e if k.startswith("CURSOR_")):      return "cursor"
    if any(k for k in e if k.startswith("CONTINUE_")):    return "continue"
    if any(k for k in e if k.startswith("AIDER_")):       return "aider"
    return "unknown-mcp-host"


async def _resolve_identity_from_mcp(ctx: Optional[Context]) -> Optional[str]:
    """Try to pull a stable per-project identity from the MCP initialize
    handshake. Returns a key like 'root:<uri>' or 'mcp-session:<id>',
    or None if nothing usable is exposed.

    This is the canonical signal: vendor-neutral, set by the host, and
    persists across MCP child restarts (unlike CLAUDE_CODE_SESSION_ID).
    """
    if ctx is None:
        return None
    # list_roots() returns the workspace roots the host declared at init.
    # First root is typically the active project folder.
    try:
        roots = await ctx.list_roots()
        if roots:
            first = roots[0]
            uri = getattr(first, "uri", None) or getattr(first, "name", None)
            if uri:
                return f"root:{str(uri).lower()}"
    except Exception:
        pass
    # Fallback: MCP session_id (stable within a single MCP host connection,
    # changes across restarts — better than nothing, worse than a root).
    try:
        sid = getattr(ctx, "session_id", None)
        if sid:
            return f"mcp-session:{sid}"
    except Exception:
        pass
    return None


def _project_dir() -> str:
    """Best-effort detection of this MCP child's identity key.

    MCP hosts spawn their children with a generic cwd, so cwd alone can't
    distinguish sessions. Priority order:
      1. AI_MESH_INSTANCE_KEY — explicit tag in the host's mcp-server env block
                                (most stable; survives host restarts)
      2. Any per-session-id env var from _SESSION_ID_VARS (vendor-specific;
                                auto-distinguishes parallel sessions but
                                changes on each host restart)
      3. Any project-dir env var from _PROJECT_DIR_VARS
      4. Path.cwd() — last resort

    Users can also call set_project_id() at runtime to override per session.
    """
    explicit = os.environ.get("AI_MESH_INSTANCE_KEY")
    if explicit:
        return f"project:{explicit.strip()}"
    for var in _SESSION_ID_VARS:
        v = os.environ.get(var)
        if v:
            return f"session:{v.strip()}"
    for var in _PROJECT_DIR_VARS:
        v = os.environ.get(var)
        if v:
            return _normalize_path(v)
    return _normalize_path(str(Path.cwd()))


def _normalize_path(p: str) -> str:
    """Return a canonical, case-consistent path so Windows case-insensitivity
    doesn't fragment config slots (C:\\WINDOWS\\system32 vs C:\\Windows\\System32)."""
    try:
        resolved = Path(p).resolve()
        if sys.platform == "win32":
            # On Windows, lowercase the whole path for stable slot keys
            return str(resolved).lower()
        return str(resolved)
    except Exception:
        return p


# Key used to isolate this instance's config from others on the same machine.
# Uses the detected project dir so each project gets its own identity. Two
# Claude Code sessions in the same project share an identity (intentional).
_CWD_KEY: str = _project_dir()

mcp = FastMCP("AI Mesh")

# ---------------------------------------------------------------------------
# Persistent config (api_key + instance_id survive restarts)
# Each cwd gets its own slot in the config file so multiple instances on the
# same machine don't collide.
# ---------------------------------------------------------------------------

def _load_cfg() -> dict:
    if CONFIG_FILE.exists():
        try:
            all_cfg = json.loads(CONFIG_FILE.read_text())
            return all_cfg.get(_CWD_KEY, {})
        except Exception:
            pass
    return {}


def _save_cfg(data: dict):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        all_cfg = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    except Exception:
        all_cfg = {}
    all_cfg[_CWD_KEY] = data
    CONFIG_FILE.write_text(json.dumps(all_cfg, indent=2))


# ---------------------------------------------------------------------------
# Runtime state (reset on each process start)
# ---------------------------------------------------------------------------

_cfg: dict = {}
_http: Optional[httpx.AsyncClient] = None
_inbox: list[dict] = []       # push messages buffered from WebSocket
_connected: bool = False


def _get_http() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(base_url=SERVER_URL, timeout=10.0)
    return _http


def _headers() -> dict:
    h: dict = {}
    api_key = _cfg.get("api_key", "")
    if api_key:
        h["X-API-Key"] = api_key
    iid = _cfg.get("instance_id", "")
    if iid:
        h["X-Instance-Id"] = iid
    return h


def _system_info() -> dict:
    pid_tag = _cfg.get("project_id")
    cwd_val = f"project:{pid_tag}" if pid_tag else _project_dir()
    info = {
        "hostname": socket.gethostname(),
        "platform": platform.system(),
        "platform_version": platform.version(),
        "python": platform.python_version(),
        "user": os.environ.get("USERNAME") or os.environ.get("USER", "unknown"),
        "cwd": cwd_val,
        "process_cwd": str(Path.cwd()),
        "pid": os.getpid(),
    }
    if pid_tag:
        info["project_id"] = pid_tag
    info["host_tool"] = _detect_host_tool()
    # Surface whichever session-id env was used (if any), keyed by its var name
    for var in _SESSION_ID_VARS:
        v = os.environ.get(var)
        if v:
            info["session_id_var"] = var
            info["session_id"]     = v
            break
    return info


# ---------------------------------------------------------------------------
# Tray auto-launch
# ---------------------------------------------------------------------------

TRAY_PID_FILE: Path = Path.home() / ".ai-mesh" / "tray.pid"


def _tray_alive() -> bool:
    """Return True if the PID file points at a running process."""
    if not TRAY_PID_FILE.exists():
        return False
    try:
        pid = int(TRAY_PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False  # PID gone
    except PermissionError:
        return True   # exists but owned by another user — still "alive"
    except OSError:
        return False
    return True


def _tray_spawn_env() -> dict:
    """Build the env for the tray subprocess.

    Override TCL_LIBRARY / TK_LIBRARY to paths derived from sys.executable —
    stale system-wide values (e.g. left by other apps like CSR BlueSuite)
    will otherwise crash tkinter inside the tray.
    """
    env = os.environ.copy()
    py_dir = Path(sys.executable).parent
    tcl_root = py_dir / "tcl"
    if tcl_root.exists():
        for sub in tcl_root.iterdir():
            if not sub.is_dir():
                continue
            n = sub.name.lower()
            if n.startswith("tcl") and (sub / "init.tcl").exists():
                env["TCL_LIBRARY"] = str(sub)
            elif n.startswith("tk") and (sub / "tk.tcl").exists():
                env["TK_LIBRARY"] = str(sub)
    return env


def _ensure_tray_running() -> None:
    """Spawn tray_app.py detached if it isn't already running. Never raises."""
    if _cfg.get("auto_tray") is False:
        return
    if _tray_alive():
        return
    tray_path = Path(__file__).parent / "tray_app.py"
    if not tray_path.exists():
        return
    # Log to a file so silent crashes are diagnosable.
    log_path = TRAY_PID_FILE.parent / "tray.log"
    try:
        TRAY_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        log_fp = open(log_path, "ab")
    except Exception:
        log_fp = subprocess.DEVNULL
    try:
        env = _tray_spawn_env()
        if sys.platform == "win32":
            DETACHED_PROCESS  = 0x00000008
            CREATE_NO_WINDOW  = 0x08000000
            proc = subprocess.Popen(
                [sys.executable, str(tray_path)],
                creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=log_fp,
                stderr=log_fp,
                env=env,
            )
        else:
            proc = subprocess.Popen(
                [sys.executable, str(tray_path)],
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=log_fp,
                stderr=log_fp,
                env=env,
            )
        TRAY_PID_FILE.write_text(str(proc.pid))
    except Exception:
        # Never let tray-spawn failure break connect()
        pass


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def _heartbeat_loop():
    http = _get_http()
    while True:
        await asyncio.sleep(15)
        try:
            await http.post(
                "/api/heartbeat",
                headers=_headers(),
                json={"hook_mode": _cfg.get("hook_mode", "off")},
            )
        except Exception:
            pass


def _write_incoming(msg: dict):
    """Append a message to incoming.json for Claude Code hook scripts to read."""
    try:
        INCOMING_FILE.parent.mkdir(parents=True, exist_ok=True)
        existing = json.loads(INCOMING_FILE.read_text()) if INCOMING_FILE.exists() else []
        if not isinstance(existing, list):
            existing = []
        existing.append(msg)
        INCOMING_FILE.write_text(json.dumps(existing, indent=2))
    except Exception:
        pass


async def _ws_listener():
    """Maintain a persistent WebSocket to receive push messages."""
    api_key = _cfg.get("api_key", "")
    iid     = _cfg.get("instance_id", "")
    ws_base = SERVER_URL.replace("http://", "ws://").replace("https://", "wss://")
    url = f"{ws_base}/ws/instance?api_key={api_key}&instance_id={iid}"
    while True:
        try:
            async with websockets.connect(url) as ws:
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if msg.get("type") == "ping":
                        await ws.send(json.dumps({"type": "pong"}))
                    elif msg.get("type") == "set_hook_mode":
                        mode = msg.get("mode")
                        if mode in HOOK_MODES:
                            _cfg["hook_mode"] = mode
                            _save_cfg(_cfg)
                    elif msg.get("type") == "message":
                        _inbox.append(msg)
                        # Cap buffer at 200 messages
                        if len(_inbox) > 200:
                            _inbox.pop(0)
                        # Write to incoming.json for hook scripts
                        _write_incoming(msg)
        except Exception:
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def connect(name: str = "", instance_type: str = "claude-code", fresh: bool = False, ctx: Context = None) -> str:
    """
    Register this instance with the AI Mesh server.
    Call this first before using any other tool.
    Requires an API key — call set_api_key() first if you don't have one yet.

    Args:
        name: Display name for this instance. Defaults to hostname-pid.
        instance_type: Type label, e.g. 'claude-code', 'claude-api', 'worker'.
        fresh: If True, force a brand-new instance row even if the server
               already has one matching (owner, hostname, cwd). Use when
               you want a distinct identity for this session and the
               server-side dedup keeps collapsing you onto an existing one
               (e.g. multiple Claude sessions in the same project dir).
               Wipes any saved instance_id locally before registering.
    """
    global _cfg, _connected, _CWD_KEY

    # If the env-var chain didn't give us a real identity key (we landed
    # on cwd fallback), try the MCP initialize handshake. Roots from
    # ctx.list_roots() are the vendor-neutral project signal we actually want.
    if ctx is not None and not os.environ.get("AI_MESH_INSTANCE_KEY"):
        # Only override when the current slot key looks like a cwd-fallback
        # (i.e. not already a 'project:' / 'session:' / 'root:' tagged key).
        if not any(_CWD_KEY.startswith(p) for p in ("project:", "session:", "root:", "mcp-session:")):
            mcp_key = await _resolve_identity_from_mcp(ctx)
            if mcp_key and mcp_key != _CWD_KEY:
                # Migrate this process's slot to the new key (if no existing
                # data lives there) and reload _cfg accordingly.
                all_cfg = {}
                try:
                    if CONFIG_FILE.exists():
                        all_cfg = json.loads(CONFIG_FILE.read_text())
                except Exception:
                    pass
                if mcp_key not in all_cfg and _CWD_KEY in all_cfg:
                    all_cfg[mcp_key] = all_cfg.pop(_CWD_KEY)
                    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
                    CONFIG_FILE.write_text(json.dumps(all_cfg, indent=2))
                _CWD_KEY = mcp_key

    _cfg = _load_cfg()
    http = _get_http()

    # API key is required
    if not _cfg.get("api_key"):
        return (
            "No API key configured.\n"
            f"1. Open the web GUI at {SERVER_URL}\n"
            "2. Log in and go to your account → API Keys → Generate Key\n"
            "3. Copy the key (shown only once) and run: set_api_key('mesh_...')"
        )

    # If fresh=True, drop any saved instance_id and force a new server-side row
    if fresh:
        _cfg.pop("instance_id", None)

    # Try to reconnect if already registered (skipped when fresh=True)
    if _cfg.get("instance_id"):
        try:
            r = await http.post("/api/heartbeat", headers=_headers())
            if r.status_code == 200:
                _connected = True
                asyncio.create_task(_heartbeat_loop())
                asyncio.create_task(_ws_listener())
                _ensure_tray_running()
                iid = _cfg["instance_id"]
                stored_name = _cfg.get("name", iid)
                return (
                    f"Reconnected to AI Mesh as '{stored_name}' "
                    f"(ID: {iid}) at {SERVER_URL}"
                )
        except Exception:
            pass

    # Even without a saved instance_id, the server may already have an
    # instance for us (e.g. config was wiped but the row persists). Look
    # it up by (api_key owner, matching identity_key in system_info.cwd)
    # before creating yet another duplicate. Honors fresh=True bypass.
    if not fresh:
        sys_info = _system_info()
        try:
            r = await http.get(
                "/api/instances",
                headers={"X-API-Key": _cfg.get("api_key", "")},
            )
            if r.status_code == 200:
                wanted_cwd = sys_info.get("cwd")
                wanted_host = sys_info.get("hostname")
                for inst in r.json():
                    si_raw = inst.get("system_info") or "{}"
                    try:
                        si = json.loads(si_raw) if isinstance(si_raw, str) else si_raw
                    except Exception:
                        continue
                    if (si.get("cwd") == wanted_cwd
                            and si.get("hostname") == wanted_host):
                        # Adopt this existing instance.
                        _cfg["instance_id"] = inst["id"]
                        _cfg["name"] = inst.get("name") or _cfg.get("name", "")
                        _save_cfg(_cfg)
                        _connected = True
                        asyncio.create_task(_heartbeat_loop())
                        asyncio.create_task(_ws_listener())
                        _ensure_tray_running()
                        return (
                            f"Adopted existing AI Mesh instance "
                            f"'{_cfg['name']}' (ID: {inst['id']}) — local "
                            f"config was missing the link."
                        )
        except Exception:
            pass

    # Fresh registration
    if not name:
        name = f"{socket.gethostname()}-{os.getpid()}"

    register_body: dict = {
        "name": name,
        "instance_type": instance_type,
        "system_info": _system_info(),
    }
    if fresh:
        register_body["force_new"] = True
    r = await http.post(
        "/api/register",
        headers=_headers(),
        json=register_body,
    )
    r.raise_for_status()
    data = r.json()

    _cfg.update({
        "instance_id": data["instance_id"],
        "name": name,
        "server_url": SERVER_URL,
    })
    _save_cfg(_cfg)
    _connected = True

    asyncio.create_task(_heartbeat_loop())
    asyncio.create_task(_ws_listener())
    _ensure_tray_running()

    return (
        f"Connected to AI Mesh as '{name}' "
        f"(ID: {data['instance_id']}) at {SERVER_URL}\n"
        f"Config saved to {CONFIG_FILE}"
    )


@mcp.tool()
async def send_message(content: str, to_instance_id: str = "", as_agent: str = "") -> str:
    """
    Send a message to another instance or broadcast to all.

    Args:
        content: The message text.
        to_instance_id: Target instance ID. Leave empty to broadcast to everyone.
        as_agent: Optional sub-agent persona tag (e.g. 'Buyer Agent').
                  Recipients see the sender as 'YourName / as_agent'.
                  No new mesh instance is created — your main instance
                  remains the only mesh participant.
    """
    if not _cfg.get("api_key"):
        return "Not connected. Call connect() first."

    http = _get_http()
    payload: dict = {"content": content, "to_id": to_instance_id or None}
    if as_agent:
        payload["as_agent"] = as_agent
    r = await http.post("/api/messages", headers=_headers(), json=payload)
    r.raise_for_status()

    target = f"instance {to_instance_id}" if to_instance_id else "all instances (broadcast)"
    tag = f" as '{as_agent}'" if as_agent else ""
    return f"Message sent to {target}{tag}."


@mcp.tool()
async def check_inbox() -> str:
    """
    Return all push messages that arrived since the last call, then clear the buffer.
    This is the fastest way to see if another instance has messaged you.
    """
    if not _inbox:
        return "Inbox is empty."

    msgs = list(_inbox)
    _inbox.clear()

    lines = []
    for m in msgs:
        ts = time.strftime("%H:%M:%S", time.localtime(m.get("timestamp", 0)))
        sender = m.get("from_name") or m.get("from_id", "?")
        fid = m.get("from_id", "?")
        fagent = m.get("from_agent")
        to = m.get("to_id")
        target = f"→ {to}" if to else "(broadcast)"
        agent_tag = f" [agent={fagent}]" if fagent else ""
        lines.append(f"[{ts}] {sender} [from_id={fid}]{agent_tag} {target}: {m.get('content','')}")
    lines.append("\nTo reply, call send_message(content, to_instance_id=<from_id>). To reply as a sub-agent, also pass as_agent='YourAgentName'.")

    return "\n".join(lines)


@mcp.tool()
async def get_messages(limit: int = 20) -> str:
    """
    Fetch recent message history from the server for this instance (includes broadcasts).

    Args:
        limit: Max number of messages to return (newest first).
    """
    if not _cfg.get("api_key"):
        return "Not connected. Call connect() first."

    http = _get_http()
    r = await http.get(f"/api/messages?limit={limit}", headers=_headers())
    r.raise_for_status()
    msgs = r.json()

    if not msgs:
        return "No messages found."

    lines = []
    for m in msgs:
        ts = time.strftime("%H:%M:%S", time.localtime(m.get("timestamp", 0)))
        sender = m.get("from_display") or m.get("from_name") or m.get("from_id", "?")
        fid = m.get("from_id", "?")
        to = m.get("to_id")
        target = f"→ {to}" if to else "(broadcast)"
        lines.append(f"[{ts}] {sender} [from_id={fid}] {target}: {m.get('content','')}")

    return "\n".join(lines)


@mcp.tool()
async def list_instances() -> str:
    """
    List all instances registered with the mesh server (online and offline).
    """
    if not _cfg.get("api_key"):
        return "Not connected. Call connect() first."

    http = _get_http()
    r = await http.get("/api/instances", headers=_headers())
    r.raise_for_status()
    instances = r.json()

    if not instances:
        return "No instances registered."

    my_id = _cfg.get("instance_id", "")
    lines = []
    for i in instances:
        if i["id"] == "admin":
            continue
        status = "🟢" if i["connected"] else "⚫"
        name = i.get("display_name") or i["name"]
        sys_info = json.loads(i.get("system_info") or "{}")
        host = sys_info.get("hostname", "")
        me = " (you)" if i["id"] == my_id else ""
        lines.append(
            f"{status} {name}{me}  ID:{i['id']}  [{i['instance_type']}]  {host}"
        )

    return "\n".join(lines)


@mcp.tool()
async def delete_instance(instance_id: str) -> str:
    """
    Delete an instance from the mesh server. You can only delete instances
    you own (or, if you're an admin, any instance). The MCP client process
    behind a deleted instance will need to re-register on its next connect().

    Args:
        instance_id: ID from list_instances() — e.g. '46a60808'.
    """
    if not _cfg.get("api_key"):
        return "Not connected. Call connect() first."
    if instance_id == "admin":
        return "Cannot delete the admin pseudo-instance."

    http = _get_http()
    r = await http.delete(
        f"/api/instances/{instance_id}",
        headers=_headers(),
    )
    if r.status_code == 200:
        return f"Deleted instance {instance_id}."
    try:
        msg = r.json().get("detail", r.text)
    except Exception:
        msg = r.text
    return f"Delete failed ({r.status_code}): {msg}"


@mcp.tool()
async def cleanup_stale_instances(offline_only: bool = True, keep_self: bool = True, dry_run: bool = False) -> str:
    """
    Sweep your owned instances and delete the ones that look stale.

    Args:
        offline_only: If True (default), only delete instances whose
            connected flag is 0. Set False to delete EVERY owned instance
            (you'll still be excluded by keep_self).
        keep_self: If True (default), spare this process's own instance_id
            from deletion.
        dry_run: If True, list what WOULD be deleted without doing it.
    """
    if not _cfg.get("api_key"):
        return "Not connected. Call connect() first."

    http = _get_http()
    r = await http.get("/api/instances", headers=_headers())
    if r.status_code != 200:
        return f"Could not list instances: {r.status_code} {r.text}"
    instances = r.json()

    my_id = _cfg.get("instance_id", "")
    # Owned instances are the ones visible under our api_key that aren't
    # the admin pseudo-instance and aren't from another user's public set.
    # The /api/instances endpoint already filters by visibility, so we
    # further restrict to rows whose owner_id is ours when known. Without
    # an explicit owner_id field on each row, we use hostname-matching as
    # a heuristic: stale rows on OTHER hosts are not ours to clean.
    sys_info = _system_info()
    my_host  = sys_info.get("hostname", "")

    candidates = []
    for i in instances:
        if i["id"] == "admin":
            continue
        if keep_self and i["id"] == my_id:
            continue
        if offline_only and i.get("connected"):
            continue
        si_raw = i.get("system_info") or "{}"
        try:
            si = json.loads(si_raw) if isinstance(si_raw, str) else si_raw
        except Exception:
            si = {}
        # Only sweep rows that came from THIS machine (defensive)
        if si.get("hostname") != my_host:
            continue
        candidates.append(i)

    if not candidates:
        return "Nothing to clean — no matching stale instances on this host."

    lines = []
    for i in candidates:
        name = i.get("display_name") or i["name"]
        marker = "🟢" if i.get("connected") else "⚫"
        lines.append(f"  {marker} {name}  [{i['id']}]")

    if dry_run:
        return "Would delete:\n" + "\n".join(lines) + "\n\n(dry_run=True — pass dry_run=False to actually delete.)"

    deleted, failed = [], []
    for i in candidates:
        rr = await http.delete(f"/api/instances/{i['id']}", headers=_headers())
        (deleted if rr.status_code == 200 else failed).append(i["id"])

    out = [f"Deleted {len(deleted)} instance(s)."]
    if deleted:
        out.append("Removed: " + ", ".join(deleted))
    if failed:
        out.append("Failed: " + ", ".join(failed))
    return "\n".join(out)


@mcp.tool()
async def list_local_slots() -> str:
    """
    Show every config slot in ~/.ai-mesh/config.json on this machine,
    with which one this process is currently bound to. Useful for spotting
    stale slots created by older code or wiped registrations.
    """
    try:
        all_cfg = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    except Exception as e:
        return f"Could not read {CONFIG_FILE}: {e}"
    if not all_cfg:
        return "No slots in config.json."
    lines = [f"{len(all_cfg)} slot(s) in {CONFIG_FILE}:"]
    for cwd, cfg in all_cfg.items():
        marker  = "  ◀ this process" if cwd == _CWD_KEY else ""
        iid     = cfg.get("instance_id", "(none)")
        name    = cfg.get("name", "(no name)")
        api_key = cfg.get("api_key", "")
        key_disp = (api_key[:13] + "…") if api_key else "(none)"
        lines.append(f"  [{iid}] {name}{marker}")
        lines.append(f"    cwd:     {cwd}")
        lines.append(f"    api_key: {key_disp}")
    return "\n".join(lines)


@mcp.tool()
async def delete_local_slot(slot_key: str) -> str:
    """
    Remove a slot from this machine's ~/.ai-mesh/config.json. Use this to
    clear stale local entries (e.g. older cwd-based slots that have been
    superseded by 'root:' or 'project:' tagged ones). Does NOT delete the
    server-side instance — use delete_instance() for that.

    Args:
        slot_key: The exact cwd / project: / root: key. Get them from
                  list_local_slots().
    """
    if slot_key == _CWD_KEY:
        return f"Refusing to delete the slot this process is using ({slot_key})."
    try:
        all_cfg = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    except Exception as e:
        return f"Could not read {CONFIG_FILE}: {e}"
    if slot_key not in all_cfg:
        return f"No such slot: {slot_key}"
    all_cfg.pop(slot_key)
    CONFIG_FILE.write_text(json.dumps(all_cfg, indent=2))
    return f"Removed local slot '{slot_key}'."


@mcp.tool()
async def set_name(name: str) -> str:
    """
    Update this instance's display name on the mesh server.

    Args:
        name: New name for this instance.
    """
    if not _cfg.get("api_key"):
        return "Not connected. Call connect() first."

    iid = _cfg["instance_id"]
    http = _get_http()
    r = await http.patch(
        f"/api/instances/{iid}",
        headers=_headers(),
        json={"name": name},
    )
    r.raise_for_status()
    _cfg["name"] = name
    _save_cfg(_cfg)
    return f"Name updated to '{name}'."


@mcp.tool()
async def my_info(ctx: Context = None) -> str:
    """
    Show this instance's registration + identity-detection details.
    Helps diagnose dedup/identity issues across multiple MCP hosts.
    """
    api_key = _cfg.get("api_key", "")
    key_display = (api_key[:13] + "...") if api_key else "(none)"

    # Identity-detection chain
    detected_id_key = _project_dir()
    explicit_tag    = os.environ.get("AI_MESH_INSTANCE_KEY")
    session_var: Optional[str] = None
    session_val: Optional[str] = None
    for v in _SESSION_ID_VARS:
        if os.environ.get(v):
            session_var, session_val = v, os.environ.get(v)
            break

    # MCP-handshake signals (available only when called with a Context)
    mcp_roots: list = []
    mcp_session_id = None
    if ctx is not None:
        try:
            mcp_session_id = getattr(ctx, "session_id", None)
        except Exception:
            pass
        try:
            for r in await ctx.list_roots():
                mcp_roots.append(getattr(r, "uri", None) or getattr(r, "name", None) or str(r))
        except Exception:
            pass

    return json.dumps(
        {
            "instance_id":  _cfg.get("instance_id"),
            "name":         _cfg.get("name"),
            "server_url":   _cfg.get("server_url", SERVER_URL),
            "api_key":      key_display,
            "connected":    _connected,
            "config_file":  str(CONFIG_FILE),
            "config_slot":  _CWD_KEY,
            "identity_key": detected_id_key,
            "host_tool":    _detect_host_tool(),
            "explicit_tag (AI_MESH_INSTANCE_KEY)": explicit_tag,
            "session_id_var":  session_var,
            "session_id":      session_val,
            "process_cwd":     str(Path.cwd()),
            "mcp_session_id":  mcp_session_id,
            "mcp_roots":       mcp_roots,
        },
        indent=2,
    )


@mcp.tool()
async def set_api_key(key: str) -> str:
    """
    Store an API key for authenticating with the AI Mesh server.
    Generate a key from the web GUI: Log in → API Keys → Generate Key.
    The key is stored in config and used for all subsequent requests.

    Args:
        key: API key starting with 'mesh_'.
    """
    global _cfg
    if not key.startswith("mesh_"):
        return "Invalid key format. Keys must start with 'mesh_'."
    _cfg = _load_cfg()
    _cfg["api_key"] = key
    # Keep any existing instance_id — connect() heartbeats with the new key
    # first; if the server says the instance belongs to a different user,
    # it falls through to a fresh /api/register. Same-user rotations
    # therefore reuse the existing instance row.
    _save_cfg(_cfg)
    prefix = key[:13] + "..."
    return (
        f"API key saved ({prefix}). Call connect() — reuses your existing "
        f"instance if the new key shares its owner, otherwise registers fresh."
    )


HOOK_MODES = ("off", "prompt", "tool", "both")


@mcp.tool()
async def set_hook_mode(mode: str) -> str:
    """
    Set the Claude Code hook injection mode for incoming AI Mesh messages.

    Modes:
      off    - hooks disabled, no injection (default)
      prompt - inject pending messages at the top of your next prompt (UserPromptSubmit)
      tool   - inject pending messages after each tool use (PostToolUse)
      both   - inject on both events

    Args:
        mode: One of 'off', 'prompt', 'tool', 'both'.
    """
    if mode not in HOOK_MODES:
        return f"Invalid mode '{mode}'. Choose from: {', '.join(HOOK_MODES)}"
    _cfg["hook_mode"] = mode
    _save_cfg(_cfg)
    if mode == "off":
        return "Hook injection disabled. Messages still buffered in check_inbox()."
    return f"Hook mode set to '{mode}'. Incoming messages will be injected into your Claude Code session."


@mcp.tool()
async def set_project_id(project_id: str) -> str:
    """
    Tag this Claude session with an explicit project identifier so multiple
    sessions on the same machine don't collapse into a single shared
    instance. Useful when Claude Code Desktop doesn't expose a per-project
    env var to the MCP child.

    Migrates this process's config slot to a new key and clears the saved
    instance_id, so the next connect() registers a fresh, distinct instance
    keyed on (owner, hostname, project_id) — the server-side dedup will
    then keep this session separate from any other project on this machine.

    For a permanent setup, also set AI_MESH_INSTANCE_KEY in the env block
    of each project's .claude/settings.json mcp-server config.

    Args:
        project_id: A short identifier (e.g. 'projedex-app', 'ai-mesh').
    """
    global _cfg, _CWD_KEY
    project_id = project_id.strip()
    if not project_id:
        return "Provide a non-empty project_id."

    new_key = f"project:{project_id}"
    old_key = _CWD_KEY

    # Load the whole config, migrate the current slot to the new key if
    # the destination is empty.
    all_cfg: dict = {}
    if CONFIG_FILE.exists():
        try:
            all_cfg = json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass

    current = all_cfg.get(old_key, {})
    target  = all_cfg.get(new_key, {})

    if old_key != new_key and current and not target:
        all_cfg[new_key] = current
        all_cfg.pop(old_key, None)
        target = current

    target["project_id"] = project_id
    target.pop("instance_id", None)  # force fresh registration

    all_cfg[new_key] = target
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(all_cfg, indent=2))

    _CWD_KEY = new_key
    _cfg = target
    return (
        f"Project ID set to '{project_id}'. Config slot moved to '{new_key}'. "
        f"Call connect() to register a fresh instance under this identity."
    )


@mcp.tool()
async def launch_tray(force: bool = False) -> str:
    """
    Launch the AI Mesh system-tray app (tray_app.py) if it isn't already
    running. The tray shows status, lets you toggle hook mode, view inbox,
    and inspect all locally-registered instances.

    Args:
        force: If True, ignore the PID file and spawn a new tray process
               anyway (useful if the previous tray crashed without cleaning up).
    """
    if force:
        try:
            TRAY_PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
    elif _tray_alive():
        try:
            pid = TRAY_PID_FILE.read_text().strip()
        except Exception:
            pid = "?"
        return f"Tray already running (PID {pid}). Pass force=True to spawn another."

    _ensure_tray_running()
    if _tray_alive():
        pid = TRAY_PID_FILE.read_text().strip()
        return f"Tray launched (PID {pid}). Look in your system tray."
    return (
        "Failed to spawn tray. Check that tray_app.py exists next to "
        "mcp_client.py and that pystray + Pillow are installed."
    )


@mcp.tool()
async def set_auto_tray(enabled: bool) -> str:
    """
    Enable or disable automatic launch of the system-tray app on connect().
    When enabled (default), connect() spawns tray_app.py if no tray is
    running yet. When disabled, you launch the tray manually.

    Args:
        enabled: True to auto-launch, False to opt out.
    """
    global _cfg
    _cfg = _load_cfg()
    _cfg["auto_tray"] = bool(enabled)
    _save_cfg(_cfg)
    return f"Auto-tray {'enabled' if enabled else 'disabled'} for this cwd."


@mcp.tool()
async def get_hook_mode() -> str:
    """
    Get the current Claude Code hook injection mode.
    """
    mode = _cfg.get("hook_mode", "off")
    descriptions = {
        "off":    "disabled — no automatic injection",
        "prompt": "inject on UserPromptSubmit (prepended to your next prompt)",
        "tool":   "inject on PostToolUse (after each tool call during active tasks)",
        "both":   "inject on both UserPromptSubmit and PostToolUse",
    }
    return f"Current hook mode: '{mode}' — {descriptions.get(mode, '')}"


# ---------------------------------------------------------------------------
# Unattended sub-agents
# ---------------------------------------------------------------------------

_AGENTS_DIR: Path = Path.home() / ".ai-mesh" / "agents"
# job_id -> {name, prompt, started, proc, out_path, err_path, cwd}
# In-memory only; cleared on MCP client restart. Output files persist on disk.
_agents: dict[str, dict] = {}


def _find_claude_cli() -> Optional[str]:
    """Locate the Claude Code CLI binary."""
    return shutil.which("claude") or shutil.which("claude.cmd") or shutil.which("claude.exe")


@mcp.tool()
async def spawn_agent(prompt: str, name: str = "agent", cwd: str = "") -> str:
    """
    Spin off an unattended Claude Code agent in the background. Returns a
    job_id immediately so the main session can keep working; check progress
    with list_agents() and pull the transcript with get_agent_result(job_id)
    when ready.

    The sub-agent runs `claude -p <prompt>` in print/non-interactive mode.
    It uses the same Anthropic auth as the calling user.

    Args:
        prompt: The task for the sub-agent (full instructions).
        name: Short label shown in list_agents() (display only).
        cwd: Working directory for the sub-agent (defaults to current).
    """
    if not prompt.strip():
        return "Provide a non-empty prompt."
    claude_bin = _find_claude_cli()
    if not claude_bin:
        return "Error: 'claude' CLI not found in PATH. Install Claude Code first."

    job_id = uuid.uuid4().hex[:8]
    job_dir = _AGENTS_DIR / job_id
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        out_path = job_dir / "stdout.log"
        err_path = job_dir / "stderr.log"
        prompt_path = job_dir / "prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        out_fp = open(out_path, "wb")
        err_fp = open(err_path, "wb")
    except Exception as e:
        return f"Failed to prepare agent workspace: {e}"

    work_cwd = cwd or str(Path.cwd())
    try:
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW | 0x00000008  # DETACHED_PROCESS
            proc = subprocess.Popen(
                [claude_bin, "-p", prompt],
                stdout=out_fp,
                stderr=err_fp,
                stdin=subprocess.DEVNULL,
                cwd=work_cwd,
                creationflags=creationflags,
                close_fds=True,
            )
        else:
            proc = subprocess.Popen(
                [claude_bin, "-p", prompt],
                stdout=out_fp,
                stderr=err_fp,
                stdin=subprocess.DEVNULL,
                cwd=work_cwd,
                start_new_session=True,
            )
    except Exception as e:
        return f"Failed to spawn agent: {e}"

    _agents[job_id] = {
        "name":     name,
        "prompt":   prompt,
        "started":  time.time(),
        "proc":     proc,
        "out_path": str(out_path),
        "err_path": str(err_path),
        "cwd":      work_cwd,
    }
    return (
        f"Agent '{name}' spawned (job_id={job_id}, PID={proc.pid}).\n"
        f"  list_agents() to check status.\n"
        f"  get_agent_result('{job_id}') to retrieve output.\n"
        f"  kill_agent('{job_id}') to stop it."
    )


@mcp.tool()
async def list_agents() -> str:
    """
    List all sub-agents spawned this MCP session, with status and elapsed time.
    """
    if not _agents:
        return "No agents spawned this session."
    lines = []
    for jid, info in _agents.items():
        rc = info["proc"].poll()
        status = "🟢 running" if rc is None else f"⚫ done (exit {rc})"
        elapsed = int(time.time() - info["started"])
        snippet = info["prompt"].replace("\n", " ")[:60]
        lines.append(f"[{jid}] {info['name']} — {status}, {elapsed}s  »  {snippet}…")
    return "\n".join(lines)


@mcp.tool()
async def get_agent_result(job_id: str, tail_lines: int = 0) -> str:
    """
    Fetch a sub-agent's transcript (stdout + stderr) plus exit status.

    Args:
        job_id: ID returned by spawn_agent().
        tail_lines: If >0, return only the last N lines of stdout (handy for
                    long-running agents). Default 0 = full output.
    """
    info = _agents.get(job_id)
    if not info:
        return f"No agent with job_id '{job_id}'. (Spawned agents are lost on MCP restart.)"
    rc = info["proc"].poll()
    status = "running" if rc is None else f"done (exit {rc})"
    try:
        out = Path(info["out_path"]).read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        out = f"(could not read stdout: {e})"
    if tail_lines > 0 and out:
        out = "\n".join(out.splitlines()[-tail_lines:])
    try:
        err = Path(info["err_path"]).read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        err = ""
    parts = [
        f"Agent: {info['name']}  (job_id={job_id}, status={status})",
        f"Started: {time.strftime('%H:%M:%S', time.localtime(info['started']))}",
        f"Prompt: {info['prompt'][:200]}{'…' if len(info['prompt']) > 200 else ''}",
        "",
        "── Output ──",
        out if out else "(empty)",
    ]
    if err:
        parts += ["", "── Stderr ──", err]
    return "\n".join(parts)


@mcp.tool()
async def kill_agent(job_id: str) -> str:
    """
    Terminate a running sub-agent. No-op if already finished.

    Args:
        job_id: ID returned by spawn_agent().
    """
    info = _agents.get(job_id)
    if not info:
        return f"No agent with job_id '{job_id}'."
    proc = info["proc"]
    if proc.poll() is not None:
        return f"Agent {job_id} already finished (exit {proc.returncode})."
    try:
        proc.terminate()
        time.sleep(0.5)
        if proc.poll() is None:
            proc.kill()
        return f"Agent {job_id} terminated."
    except Exception as e:
        return f"Failed to terminate: {e}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
