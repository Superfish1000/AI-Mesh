"""
AI Mesh Tray App
System tray icon with configuration panel for the AI Mesh MCP client.
Reads/writes the same ~/.ai-mesh/config.json used by mcp_client.py.

Run standalone:
    python tray_app.py

Dependencies: pystray, Pillow (see requirements.txt)
tkinter is included with Python on all platforms.
"""

import json
import os
import platform
import queue
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from tkinter import (
    END, BooleanVar, Frame, Label, OptionMenu, StringVar, Text, Tk, Toplevel,
    messagebox, scrolledtext,
)
import tkinter as tk
import tkinter.ttk as ttk

import httpx
import pystray
from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# Config helpers (shared with mcp_client.py)
# ---------------------------------------------------------------------------

CONFIG_FILE = Path.home() / ".ai-mesh" / "config.json"
SERVER_URL_DEFAULT = "http://localhost:8000"

# The tray app manages ALL cwd-keyed configs so operators can see and switch
# between every registered instance on this machine. If the launch cwd has no
# api_key, fall back to the first registered slot that does — so users don't
# get "not configured" just because they launched the tray from a fresh shell.
_CWD_KEY: str = str(Path.cwd())


def _pick_active_cwd() -> str:
    """Pick the cwd slot the tray should bind to.

    Prefer this process's cwd; otherwise pick the first slot in the config
    file that has an api_key. Returns the launch cwd if nothing is configured.
    """
    if not CONFIG_FILE.exists():
        return _CWD_KEY
    try:
        all_cfg = json.loads(CONFIG_FILE.read_text())
    except Exception:
        return _CWD_KEY
    if all_cfg.get(_CWD_KEY, {}).get("api_key"):
        return _CWD_KEY
    for cwd, cfg in all_cfg.items():
        if isinstance(cfg, dict) and cfg.get("api_key"):
            return cwd
    return _CWD_KEY


_CWD_KEY = _pick_active_cwd()

INSTANCE_TYPES = [
    "claude-code",
    "claude-api",
    "worker",
    "reviewer",
    "tester",
    "human",
    "other",
]


def _read_all_cfg() -> dict:
    """Return the full config file as a dict-of-dicts (keyed by cwd)."""
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return {}


def load_cfg(cwd_key: str = _CWD_KEY) -> dict:
    """Load the config for a specific cwd (defaults to this process's cwd)."""
    return _read_all_cfg().get(cwd_key, {})


def save_cfg(data: dict, cwd_key: str = _CWD_KEY):
    """Write config for a specific cwd without touching other entries."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    all_cfg = _read_all_cfg()
    all_cfg[cwd_key] = data
    CONFIG_FILE.write_text(json.dumps(all_cfg, indent=2))


def system_info() -> dict:
    return {
        "hostname": socket.gethostname(),
        "platform": platform.system(),
        "platform_version": platform.version(),
        "python": platform.python_version(),
        "user": os.environ.get("USERNAME") or os.environ.get("USER", "unknown"),
        "cwd": str(Path.cwd()),
        "pid": os.getpid(),
    }


# ---------------------------------------------------------------------------
# Tray icon images (generated with Pillow — no external assets needed)
# ---------------------------------------------------------------------------

_ICON_SIZE = 64


def _make_icon(color: str) -> Image.Image:
    img = Image.new("RGBA", (_ICON_SIZE, _ICON_SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Dark background circle
    d.ellipse([2, 2, _ICON_SIZE - 2, _ICON_SIZE - 2], fill="#1a1a2e")
    # Status dot (outer glow + fill)
    cx, cy, r = _ICON_SIZE // 2, _ICON_SIZE // 2, _ICON_SIZE // 2 - 8
    d.ellipse([cx - r - 3, cy - r - 3, cx + r + 3, cy + r + 3], fill=color + "40")
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    # "M" letter in white
    d.text((cx - 8, cy - 10), "M", fill="#ffffff")
    return img


ICON_CONNECTED    = _make_icon("#3fb950")   # green
ICON_DISCONNECTED = _make_icon("#f85149")   # red
ICON_CONNECTING   = _make_icon("#d29922")   # amber


# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------

class MeshState:
    def __init__(self):
        self.cfg: dict = load_cfg()
        self.connected: bool = False
        self.instances: list[dict] = []
        self.inbox: list[dict] = []
        self._lock = threading.Lock()

    @property
    def server_url(self) -> str:
        return self.cfg.get("server_url") or self.cfg.get("AI_MESH_URL") or SERVER_URL_DEFAULT

    @property
    def api_key(self) -> str:
        return self.cfg.get("api_key", "")

    @property
    def instance_id(self) -> str:
        return self.cfg.get("instance_id", "")

    @property
    def name(self) -> str:
        return self.cfg.get("name", "")

    def headers(self) -> dict:
        return {"X-API-Key": self.api_key} if self.api_key else {}


state = MeshState()

# ---------------------------------------------------------------------------
# HTTP helpers (sync, used from background thread)
# ---------------------------------------------------------------------------

def _get(path: str) -> dict | list | None:
    try:
        r = httpx.get(state.server_url + path, headers=state.headers(), timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _post(path: str, body: dict) -> dict | None:
    try:
        r = httpx.post(state.server_url + path, json=body, headers=state.headers(), timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _patch(path: str, body: dict) -> bool:
    try:
        r = httpx.patch(state.server_url + path, json=body, headers=state.headers(), timeout=5)
        return r.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Background poller
# ---------------------------------------------------------------------------

def _poll_loop(tray_icon: pystray.Icon):
    """Heartbeat + inbox poll every 15 s; updates tray icon colour."""
    while True:
        if state.api_key:
            result = _post("/api/heartbeat", {})
            was = state.connected
            state.connected = result is not None
            if state.connected != was:
                tray_icon.icon = ICON_CONNECTED if state.connected else ICON_DISCONNECTED
                tray_icon.title = _tray_title()

            # Refresh instance list
            data = _get("/api/instances")
            if data:
                with state._lock:
                    state.instances = [i for i in data if i["id"] != "admin"]

            # Poll inbox (new messages since last fetch)
            msgs = _get(f"/api/messages?limit=10")
            if msgs:
                existing_ids = {m["id"] for m in state.inbox}
                new = [m for m in msgs if m["id"] not in existing_ids]
                if new:
                    state.inbox = (new + state.inbox)[:100]
                    # Flash tray title briefly
                    tray_icon.title = f"AI Mesh — {len(new)} new message(s)"
                    threading.Timer(4, lambda: setattr(tray_icon, "title", _tray_title())).start()
        else:
            state.connected = False
            tray_icon.icon = ICON_DISCONNECTED

        time.sleep(15)


def _tray_title() -> str:
    if not state.api_key:
        return "AI Mesh — not configured"
    if state.connected:
        return f"AI Mesh — {state.name or state.instance_id} ● online"
    return f"AI Mesh — {state.name or state.instance_id} ○ offline"


# ---------------------------------------------------------------------------
# Config / main window (tkinter)
# ---------------------------------------------------------------------------

_config_win: Toplevel | None = None
_inbox_win: Toplevel | None = None
_root: Tk | None = None  # hidden Tk root

# Tkinter requires every interpreter call (including .after) to happen on the
# main thread. pystray callbacks fire on its own worker thread, so we queue
# UI work here and drain it from the tk pump loop on the main thread.
_ui_queue: "queue.Queue[callable]" = queue.Queue()


def _on_main(func, *args, **kwargs):
    """Schedule `func(*args, **kwargs)` to run on the tk main thread."""
    _ui_queue.put(lambda: func(*args, **kwargs))


def _tk_root() -> Tk:
    global _root
    if _root is None:
        _root = Tk()
        _root.withdraw()          # hidden — only child windows are shown
        _root.protocol("WM_DELETE_WINDOW", lambda: None)
    return _root


def open_config(_icon=None, _item=None):
    # pystray callbacks fire on its worker thread; bounce to main via queue.
    if threading.current_thread() is not threading.main_thread():
        _on_main(open_config)
        return

    global _config_win
    root = _tk_root()

    if _config_win and _config_win.winfo_exists():
        _config_win.lift()
        _config_win.focus_force()
        return

    win = Toplevel(root)
    _config_win = win
    win.title("AI Mesh — Configuration")
    win.resizable(False, False)
    win.configure(bg="#0d1117")

    pad = {"padx": 12, "pady": 6}
    lbl_cfg = {"bg": "#0d1117", "fg": "#8b949e", "font": ("Segoe UI", 9)}
    val_cfg = {"bg": "#0d1117", "fg": "#e6edf3", "font": ("Segoe UI", 10, "bold")}
    entry_cfg = {
        "bg": "#161b22", "fg": "#e6edf3", "insertbackground": "#e6edf3",
        "relief": "flat", "font": ("Segoe UI", 10),
        "highlightthickness": 1, "highlightbackground": "#30363d",
        "highlightcolor": "#58a6ff",
    }
    btn_primary = {
        "bg": "#1f6feb", "fg": "#ffffff", "relief": "flat",
        "font": ("Segoe UI", 10), "cursor": "hand2", "padx": 14, "pady": 6,
        "activebackground": "#388bfd", "activeforeground": "#ffffff",
        "bd": 0,
    }
    btn_secondary = {
        "bg": "#21262d", "fg": "#e6edf3", "relief": "flat",
        "font": ("Segoe UI", 10), "cursor": "hand2", "padx": 14, "pady": 6,
        "activebackground": "#30363d", "activeforeground": "#e6edf3",
        "bd": 0,
    }
    btn_danger = {
        "bg": "#b91c1c", "fg": "#ffffff", "relief": "flat",
        "font": ("Segoe UI", 10), "cursor": "hand2", "padx": 14, "pady": 6,
        "activebackground": "#dc2626", "activeforeground": "#ffffff",
        "bd": 0,
    }

    # ── Header ────────────────────────────────────────────────────────────────
    hdr = Frame(win, bg="#161b22", pady=14)
    hdr.pack(fill="x")
    Label(hdr, text="🤖  AI Mesh", bg="#161b22", fg="#58a6ff",
          font=("Segoe UI", 15, "bold")).pack()
    Label(hdr, text="MCP Client Configuration", bg="#161b22", fg="#8b949e",
          font=("Segoe UI", 9)).pack()

    # ── Status banner ─────────────────────────────────────────────────────────
    status_color = "#3fb950" if state.connected else "#f85149"
    status_text  = f"● Connected  (ID: {state.instance_id})" if state.connected else "○ Disconnected"
    status_lbl = Label(win, text=status_text, bg="#0d1117", fg=status_color,
                       font=("Segoe UI", 10, "bold"), pady=6)
    status_lbl.pack(fill="x", padx=16)

    sep = Frame(win, bg="#21262d", height=1)
    sep.pack(fill="x", padx=16, pady=4)

    # ── Form ──────────────────────────────────────────────────────────────────
    form = Frame(win, bg="#0d1117")
    form.pack(fill="x", padx=16, pady=4)

    def row(label_text: str, row_idx: int):
        Label(form, text=label_text, **lbl_cfg, anchor="w").grid(
            row=row_idx, column=0, sticky="w", **pad)

    row("Server URL", 0)
    sv_url = StringVar(value=state.server_url)
    url_entry = tk.Entry(form, textvariable=sv_url, width=38, **entry_cfg)
    url_entry.grid(row=0, column=1, sticky="ew", **pad)

    row("Instance Name", 1)
    sv_name = StringVar(value=state.name)
    name_entry = tk.Entry(form, textvariable=sv_name, width=38, **entry_cfg)
    name_entry.grid(row=1, column=1, sticky="ew", **pad)

    row("Instance Type", 2)
    sv_type = StringVar(value=state.cfg.get("instance_type", "claude-code"))
    type_menu = OptionMenu(form, sv_type, *INSTANCE_TYPES)
    type_menu.config(bg="#161b22", fg="#e6edf3", relief="flat",
                     font=("Segoe UI", 10), highlightthickness=0,
                     activebackground="#21262d", activeforeground="#e6edf3",
                     bd=0, cursor="hand2")
    type_menu["menu"].config(bg="#161b22", fg="#e6edf3", relief="flat",
                              activebackground="#1f6feb")
    type_menu.grid(row=2, column=1, sticky="w", **pad)

    # API key row — show prefix only (mask actual key after save)
    row("API Key", 3)
    existing_key = state.api_key
    key_display = (existing_key[:13] + "...") if existing_key else ""
    sv_apikey = StringVar(value=key_display)
    apikey_frame = Frame(form, bg="#0d1117")
    apikey_frame.grid(row=3, column=1, sticky="ew", **pad)
    apikey_entry = tk.Entry(apikey_frame, textvariable=sv_apikey, width=28,
                            show="", **entry_cfg)
    apikey_entry.pack(side="left", fill="x", expand=True)
    # Clear placeholder on focus so user can paste a fresh key
    def _on_apikey_focus(_evt):
        if sv_apikey.get().endswith("..."):
            sv_apikey.set("")
    apikey_entry.bind("<FocusIn>", _on_apikey_focus)

    def do_save_apikey():
        raw = sv_apikey.get().strip()
        if not raw or raw.endswith("..."):
            set_msg("Paste a full API key first.", "#f85149")
            return
        if not raw.startswith("mesh_"):
            set_msg("Key must start with 'mesh_'.", "#f85149")
            return
        state.cfg["api_key"] = raw
        # Clear stale instance_id — will re-register on next connect
        state.cfg.pop("instance_id", None)
        save_cfg(state.cfg)
        # Show masked version
        sv_apikey.set(raw[:13] + "...")
        set_msg("API key saved. Click Connect/Re-register.", "#3fb950")

    tk.Button(apikey_frame, text="Save Key", command=do_save_apikey,
              bg="#1f6feb", fg="#fff", relief="flat", font=("Segoe UI", 9),
              cursor="hand2", padx=8, pady=3,
              activebackground="#388bfd", activeforeground="#fff",
              bd=0).pack(side="left", padx=(6, 0))

    if state.instance_id:
        row("Instance ID", 4)
        Label(form, text=state.instance_id, **val_cfg, anchor="w").grid(
            row=4, column=1, sticky="w", **pad)

    form.columnconfigure(1, weight=1)

    sep2 = Frame(win, bg="#21262d", height=1)
    sep2.pack(fill="x", padx=16, pady=4)

    # ── Status message area ───────────────────────────────────────────────────
    msg_lbl = Label(win, text="", bg="#0d1117", fg="#8b949e",
                    font=("Segoe UI", 9), wraplength=340)
    msg_lbl.pack(padx=16, pady=2)

    def set_msg(text: str, color: str = "#8b949e"):
        msg_lbl.config(text=text, fg=color)
        win.update_idletasks()

    # ── Actions ───────────────────────────────────────────────────────────────
    def do_save_and_connect():
        url = sv_url.get().strip().rstrip("/")
        name = sv_name.get().strip()
        inst_type = sv_type.get()

        if not url:
            set_msg("Server URL is required.", "#f85149")
            return
        if not state.api_key:
            set_msg("Save an API key first (generate one from the web GUI).", "#f85149")
            return
        if not name:
            name = f"{socket.gethostname()}-{os.getpid()}"
            sv_name.set(name)

        set_msg("Connecting…", "#d29922")
        btn_connect.config(state="disabled")

        def _connect():
            try:
                r = httpx.post(
                    f"{url}/api/register",
                    headers={"X-API-Key": state.api_key},
                    json={"name": name, "instance_type": inst_type, "system_info": system_info()},
                    timeout=8,
                )
                r.raise_for_status()
                data = r.json()

                new_cfg = {
                    **state.cfg,
                    "instance_id": data["instance_id"],
                    "name": name,
                    "instance_type": inst_type,
                    "server_url": url,
                }
                state.cfg = new_cfg
                save_cfg(new_cfg)
                state.connected = True

                win.after(0, lambda: [
                    set_msg(f"Connected! ID: {data['instance_id']}", "#3fb950"),
                    status_lbl.config(
                        text=f"● Connected  (ID: {data['instance_id']})",
                        fg="#3fb950"
                    ),
                    btn_connect.config(state="normal"),
                ])
            except Exception as e:
                win.after(0, lambda: [
                    set_msg(f"Failed: {e}", "#f85149"),
                    btn_connect.config(state="normal"),
                ])

        threading.Thread(target=_connect, daemon=True).start()

    def do_save_name():
        """Update name on server without re-registering."""
        name = sv_name.get().strip()
        if not name or not state.api_key:
            return
        set_msg("Saving…", "#d29922")

        def _save():
            ok = _patch(f"/api/instances/{state.instance_id}", {"name": name})
            if ok:
                state.cfg["name"] = name
                save_cfg(state.cfg)
            win.after(0, lambda: set_msg(
                "Name saved." if ok else "Save failed.", "#3fb950" if ok else "#f85149"
            ))

        threading.Thread(target=_save, daemon=True).start()

    def do_disconnect():
        state.cfg.pop("api_key", None)
        state.cfg.pop("instance_id", None)
        state.connected = False
        save_cfg(state.cfg)
        status_lbl.config(text="○ Disconnected", fg="#f85149")
        set_msg("Disconnected. Config cleared.", "#8b949e")

    def do_open_gui():
        webbrowser.open(state.server_url)

    btn_frame = Frame(win, bg="#0d1117")
    btn_frame.pack(fill="x", padx=16, pady=10)

    if state.instance_id:
        # Already registered — show save-name + disconnect options
        btn_connect = tk.Button(btn_frame)   # dummy ref for _connect lambda
        btn_connect.pack_forget()
        tk.Button(btn_frame, text="Save Name", command=do_save_name, **btn_primary).pack(
            side="left", padx=(0, 6))
        tk.Button(btn_frame, text="Re-register", command=do_save_and_connect, **btn_secondary).pack(
            side="left", padx=(0, 6))
        tk.Button(btn_frame, text="Disconnect", command=do_disconnect, **btn_danger).pack(
            side="left")
    else:
        btn_connect = tk.Button(btn_frame, text="Connect", command=do_save_and_connect, **btn_primary)
        btn_connect.pack(side="left", padx=(0, 6))

    tk.Button(btn_frame, text="Open Web GUI", command=do_open_gui, **btn_secondary).pack(
        side="right")

    sep3 = Frame(win, bg="#21262d", height=1)
    sep3.pack(fill="x", padx=16, pady=4)

    # ── Instances on this machine (from config file) ──────────────────────────
    Label(win, text="Registered on this machine", **lbl_cfg,
          font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=16, pady=(4, 2))

    local_frame = Frame(win, bg="#161b22", bd=0, relief="flat")
    local_frame.pack(fill="x", padx=16, pady=(0, 4))

    local_lbl = Label(local_frame, text="", bg="#161b22", fg="#8b949e",
                      font=("Consolas", 9), justify="left", anchor="w",
                      wraplength=340, padx=8, pady=6)
    local_lbl.pack(fill="x")

    def refresh_local():
        all_cfg = _read_all_cfg()
        if not all_cfg:
            local_lbl.config(text="None.")
            return
        lines = []
        for cwd, cfg in all_cfg.items():
            iid    = cfg.get("instance_id", "(unregistered)")
            name   = cfg.get("name", "(no name)")
            itype  = cfg.get("instance_type", "claude-code")
            server = cfg.get("server_url", "(default)")
            apikey = cfg.get("api_key", "")
            hookm  = cfg.get("hook_mode", "off")
            marker = "  ◀ this session" if cwd == _CWD_KEY else ""
            short_cwd = cwd if len(cwd) <= 56 else "…" + cwd[-54:]
            key_disp  = (apikey[:13] + "…") if apikey else "(none)"
            lines.append(
                f"[{iid}] {name}{marker}\n"
                f"  cwd:       {short_cwd}\n"
                f"  server:    {server}\n"
                f"  type:      {itype}\n"
                f"  api_key:   {key_disp}\n"
                f"  hook_mode: {hookm}"
            )
        local_lbl.config(text="\n\n".join(lines))

    refresh_local()

    # ── Server instance list (live) ───────────────────────────────────────────
    Label(win, text="All server instances", **lbl_cfg,
          font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=16, pady=(4, 2))

    inst_frame = Frame(win, bg="#161b22", bd=0, relief="flat")
    inst_frame.pack(fill="x", padx=16, pady=(0, 8))

    inst_lbl = Label(inst_frame, text="Loading…", bg="#161b22", fg="#8b949e",
                     font=("Consolas", 9), justify="left", anchor="w",
                     wraplength=340, padx=8, pady=6)
    inst_lbl.pack(fill="x")

    def refresh_instances():
        with state._lock:
            ilist = list(state.instances)
        if not ilist:
            inst_lbl.config(text="No instances yet.")
            return
        lines = []
        for i in ilist:
            dot = "●" if i["connected"] else "○"
            name = i.get("display_name") or i["name"]
            lines.append(f"{dot} {name}  [{i['id']}]  {i['instance_type']}")
        inst_lbl.config(text="\n".join(lines))
        win.after(8000, refresh_instances)

    win.after(500, refresh_instances)

    # Fetch server instances immediately in background
    def _fetch():
        data = _get("/api/instances")
        if data:
            with state._lock:
                state.instances = [i for i in data if i["id"] != "admin"]
    threading.Thread(target=_fetch, daemon=True).start()

    # ── Footer ────────────────────────────────────────────────────────────────
    foot = Frame(win, bg="#010409", pady=6)
    foot.pack(fill="x", side="bottom")
    Label(foot, text=f"Config: {CONFIG_FILE}  |  cwd: {_CWD_KEY}",
          bg="#010409", fg="#484f58", font=("Segoe UI", 8)).pack()

    win.update_idletasks()
    # Center on screen
    w, h = win.winfo_width(), win.winfo_height()
    x = (win.winfo_screenwidth()  - w) // 2
    y = (win.winfo_screenheight() - h) // 2
    win.geometry(f"+{x}+{y}")


# ---------------------------------------------------------------------------
# Inbox window
# ---------------------------------------------------------------------------

def open_inbox(_icon=None, _item=None):
    # Same queue-marshal as open_config.
    if threading.current_thread() is not threading.main_thread():
        _on_main(open_inbox)
        return

    global _inbox_win
    root = _tk_root()

    if _inbox_win and _inbox_win.winfo_exists():
        _inbox_win.lift()
        _inbox_win.focus_force()
        return

    win = Toplevel(root)
    _inbox_win = win
    win.title("AI Mesh — Inbox")
    win.geometry("480x400")
    win.configure(bg="#0d1117")

    Label(win, text="Recent Messages", bg="#0d1117", fg="#58a6ff",
          font=("Segoe UI", 12, "bold"), pady=10).pack()

    txt = scrolledtext.ScrolledText(
        win, bg="#161b22", fg="#e6edf3", insertbackground="#e6edf3",
        font=("Consolas", 9), relief="flat", wrap="word",
        highlightthickness=0, state="disabled",
    )
    txt.pack(fill="both", expand=True, padx=12, pady=(0, 8))

    btn_frame = Frame(win, bg="#0d1117")
    btn_frame.pack(fill="x", padx=12, pady=(0, 10))

    def refresh():
        def _fetch():
            data = _get("/api/messages?limit=30")
            win.after(0, lambda: _render(data or []))
        threading.Thread(target=_fetch, daemon=True).start()

    def _render(msgs):
        txt.config(state="normal")
        txt.delete("1.0", END)
        if not msgs:
            txt.insert(END, "No messages.")
        else:
            for m in reversed(msgs):
                ts = time.strftime("%H:%M:%S", time.localtime(m.get("timestamp", 0)))
                sender = m.get("from_display") or m.get("from_name") or m.get("from_id", "?")
                to = m.get("to_id")
                target = f"→ {to}" if to else "(broadcast)"
                txt.insert(END, f"[{ts}] {sender} {target}\n", "meta")
                txt.insert(END, f"  {m.get('content','')}\n\n")
        txt.tag_config("meta", foreground="#8b949e")
        txt.config(state="disabled")
        txt.see(END)

    tk.Button(
        btn_frame, text="Refresh", command=refresh,
        bg="#1f6feb", fg="#fff", relief="flat", font=("Segoe UI", 9),
        cursor="hand2", padx=12, pady=4,
        activebackground="#388bfd", activeforeground="#fff",
    ).pack(side="left")

    tk.Button(
        btn_frame, text="Open Web GUI",
        command=lambda: webbrowser.open(state.server_url),
        bg="#21262d", fg="#e6edf3", relief="flat", font=("Segoe UI", 9),
        cursor="hand2", padx=12, pady=4,
        activebackground="#30363d", activeforeground="#e6edf3",
    ).pack(side="left", padx=6)

    refresh()


# ---------------------------------------------------------------------------
# Hook mode helpers
# ---------------------------------------------------------------------------

HOOK_MODES = ("off", "prompt", "tool", "both")
HOOK_LABELS = {
    "off":    "Off",
    "prompt": "On next prompt",
    "tool":   "On tool use",
    "both":   "Both",
}


def _get_hook_mode() -> str:
    return load_cfg().get("hook_mode", "off")


def _set_hook_mode(mode: str):
    cfg = load_cfg()
    cfg["hook_mode"] = mode
    save_cfg(cfg)
    state.cfg = load_cfg()


def _hook_mode_action(mode: str):
    def action(_icon, _item):
        _set_hook_mode(mode)
        _icon.menu = _build_menu()
        _icon.update_menu()
    return action


def _hook_checked(mode: str):
    return lambda _item: _get_hook_mode() == mode


# ---------------------------------------------------------------------------
# Tray menu
# ---------------------------------------------------------------------------

def _build_menu() -> pystray.Menu:
    return pystray.Menu(
        pystray.MenuItem("AI Mesh", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Configuration…", open_config, default=True),
        pystray.MenuItem("Inbox…",         open_inbox),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Hook Injection",
            pystray.Menu(
                pystray.MenuItem(
                    "Off",
                    _hook_mode_action("off"),
                    checked=_hook_checked("off"),
                    radio=True,
                ),
                pystray.MenuItem(
                    "On next prompt (UserPromptSubmit)",
                    _hook_mode_action("prompt"),
                    checked=_hook_checked("prompt"),
                    radio=True,
                ),
                pystray.MenuItem(
                    "On tool use (PostToolUse)",
                    _hook_mode_action("tool"),
                    checked=_hook_checked("tool"),
                    radio=True,
                ),
                pystray.MenuItem(
                    "Both",
                    _hook_mode_action("both"),
                    checked=_hook_checked("both"),
                    radio=True,
                ),
            ),
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Open Web GUI",
            lambda _i, _it: webbrowser.open(state.server_url),
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", lambda icon, _: icon.stop()),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # tkinter root must be created on main thread (macOS requirement)
    _tk_root()

    icon = pystray.Icon(
        name="ai-mesh",
        icon=ICON_CONNECTED if state.api_key else ICON_DISCONNECTED,
        title=_tray_title(),
        menu=_build_menu(),
    )

    # Background poller (updates icon + inbox)
    threading.Thread(target=_poll_loop, args=(icon,), daemon=True).start()

    # On double-click / default action: open config
    # (already set via default=True on Configuration menu item)

    # Pump tkinter events on main thread; drain the UI queue so work scheduled
    # from pystray's worker thread actually executes here.
    def _tk_pump():
        try:
            root = _tk_root()
            while True:
                # Run any pending UI calls from background threads
                try:
                    while True:
                        fn = _ui_queue.get_nowait()
                        try:
                            fn()
                        except Exception as e:
                            print(f"tray UI error: {e}", file=sys.stderr)
                except queue.Empty:
                    pass
                root.update()
                time.sleep(0.05)
        except Exception:
            pass

    threading.Thread(target=icon.run, daemon=True).start()
    _tk_pump()


if __name__ == "__main__":
    main()
