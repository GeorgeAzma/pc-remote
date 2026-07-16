#!/usr/bin/env python3
"""
Personal PC Remote Control Server
=================================

A tiny, dependency-free HTTP server that lets you trigger actions on your PC
from your phone (or any device on the network). It is designed to be
*extensible*: every action is just a function decorated with ``@command``.

Quick start
-----------
    # Run it (uses the venv interpreter, no console window)
    #   .venv\\Scripts\\pythonw.exe main.py

    # From your phone, open:  http://<PC-IP>:8000/
    # Or hit an endpoint directly:  http://<PC-IP>:8000/sleep

Security
--------
Set a token so random devices on your network can't control your PC:

    set PC_API_TOKEN=some-secret   (or put it in the launcher .bat)

Then call:  http://<PC-IP>:8000/sleep?token=some-secret

Configuration (environment variables)
-------------------------------------
    PC_API_HOST   interface to bind   (default 0.0.0.0 = all interfaces)
    PC_API_PORT   listen port         (default 1024)
"""
import ctypes
import inspect
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# Hide the console window for child processes (so PowerShell doesn't flash).
CREATE_NO_WINDOW = 0x08000000
_SUBPROC_KWARGS = {"creationflags": CREATE_NO_WINDOW}

# --- System master volume via winmm (WAVE_MAPPER maps to the default device) ---
_WAVE_MAPPER = 0xFFFFFFFF  # -1: route to the system's preferred playback device


def _set_master_volume(level: int):
    v = int(max(0, min(100, level)) / 100 * 0xFFFF)
    vol = v | (v << 16)  # same level on left/right channels
    ctypes.windll.winmm.waveOutSetVolume(_WAVE_MAPPER, vol)


def _get_master_volume() -> int:
    out = ctypes.c_ulong()
    ctypes.windll.winmm.waveOutGetVolume(_WAVE_MAPPER, ctypes.byref(out))
    return int(round((out.value & 0xFFFF) / 0xFFFF * 100))


START_TIME = time.time()

HOST = os.environ.get("PC_API_HOST", "0.0.0.0")
PORT = int(os.environ.get("PC_API_PORT", "1024"))
TOKEN = os.environ.get("PC_API_TOKEN", "")

commands: dict[str, dict] = {}

# Resolve the PowerShell executable once (so subprocess works even when the
# server's PATH is minimal, e.g. launched at Windows startup).
def _find_powershell() -> str:
    import shutil
    found = shutil.which("powershell") or shutil.which("pwsh")
    if found:
        return found
    for cand in (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                 r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe"):
        if os.path.exists(cand):
            return cand
    return "powershell"

POWERSHELL = _find_powershell()


def command(name: str | None = None, description: str = "", confirm: bool = False,
             primary: bool = False, undo: bool = False, hide: bool = False,
             ping: bool = False, range: list[str] | None = None,
             tab: str = "") -> callable:
    """Register a function as a command/endpoint. confirm=True asks for a
    tap-to-confirm; primary=True pins the card to the top of the UI;
    undo=True shows a Cancel button in its result; hide=True keeps it callable
    but off the UI; ping=True measures real client<->server latency in the UI;
    range=["param"] renders that int param as a 0-100 slider in the UI;
    tab="media"|"tools"|"power" groups non-primary commands under a
    collapsible section of that name."""
    def decorator(func):
        cmd_name = (name or func.__name__).lower()
        params = []
        for pname, p in inspect.signature(func).parameters.items():
            params.append({
                "name": pname,
                "type": _type_name(p.annotation),
                "default": p.default if p.default is not inspect.Parameter.empty else None,
                "has_default": p.default is not inspect.Parameter.empty,
            })
        commands[cmd_name] = {
            "func": func,
            "description": description or (func.__doc__ or "").strip(),
            "params": params,
            "confirm": confirm,
            "primary": primary,
            "undo": undo,
            "hide": hide,
            "ping": ping,
            "range": set(range or []),
            "tab": tab,
        }
        return func
    return decorator


def _type_name(annotation) -> str:
    if annotation is bool:
        return "bool"
    if annotation is int:
        return "int"
    if annotation is float:
        return "float"
    return "str"


def _coerce(value: str, type_name: str):
    if type_name == "bool":
        return str(value).lower() in ("1", "true", "yes", "on", "y")
    if type_name == "int":
        return int(value)
    if type_name == "float":
        return float(value)
    return value


# Pending cancellable actions: cmd_name -> zero-arg cancel callback.
pending: dict[str, Callable[[], None]] = {}
_pending_lock = threading.Lock()


def _register_pending(cmd_name: str, cancel_fn: Callable[[], None]):
    with _pending_lock:
        pending[cmd_name] = cancel_fn


def _clear_pending(cmd_name: str):
    with _pending_lock:
        pending.pop(cmd_name, None)


def cancel_pending(cmd_name: str | None = None) -> bool:
    """Cancel one pending action (by name) or all of them."""
    with _pending_lock:
        names = [cmd_name] if cmd_name else list(pending)
        if not names:
            return False
        for n in names:
            fn = pending.pop(n, None)
            if fn:
                try:
                    fn()
                except Exception:
                    pass
        return True


# Stats: count incoming requests and remember the last command run.
REQUEST_COUNT = 0
LAST_COMMAND = None
_stats_lock = threading.Lock()


def _record_request():
    global REQUEST_COUNT
    with _stats_lock:
        REQUEST_COUNT += 1


def _set_last(cmd: str):
    global LAST_COMMAND
    with _stats_lock:
        LAST_COMMAND = cmd


@command("sleep", "Put the computer to sleep (optionally after N seconds).", confirm=True, primary=True, undo=True)
def sleep(seconds: int = 0):
    if seconds:
        def _fire():
            _do_sleep()
            _clear_pending("sleep")
        timer = threading.Timer(seconds, _fire)
        timer.daemon = True
        timer.start()
        _register_pending("sleep", timer.cancel)
        return {"status": "sleeping_in", "seconds": seconds}
    _do_sleep()
    return {"status": "sleeping"}


def _do_sleep():
    ctypes.windll.powrprof.SetSuspendState(0, 0, 0)


@command("hibernate", "Hibernate the computer.", confirm=True, tab="power")
def hibernate():
    ctypes.windll.powrprof.SetSuspendState(1, 0, 0)
    return {"status": "hibernating"}


@command("lock", "Lock the workstation.", primary=True)
def lock():
    ctypes.windll.user32.LockWorkStation()
    return {"status": "locked"}


@command("shutdown", "Shut the computer down (use force=true if needed).", confirm=True, primary=True, undo=True)
def shutdown(force: bool = False, seconds: int = 0):
    flags = "/s" + (" /f" if force else "")
    subprocess.run(f"shutdown {flags} /t {seconds}", shell=True, check=True)
    if seconds:
        _register_pending("shutdown", lambda: subprocess.run("shutdown /a", shell=True))
        threading.Timer(seconds, lambda: _clear_pending("shutdown")).start()
        return {"status": "shutting_down", "seconds": seconds}
    return {"status": "shutting_down", "seconds": 0}


@command("restart", "Restart the computer.", confirm=True, undo=True, tab="power")
def restart(force: bool = False, seconds: int = 0):
    flags = "/r" + (" /f" if force else "")
    subprocess.run(f"shutdown {flags} /t {seconds}", shell=True, check=True)
    if seconds:
        _register_pending("restart", lambda: subprocess.run("shutdown /a", shell=True))
        threading.Timer(seconds, lambda: _clear_pending("restart")).start()
        return {"status": "restarting", "seconds": seconds}
    return {"status": "restarting", "seconds": 0}


@command("cancel", "Cancel a pending shutdown/restart or sleep.", hide=True)
def cancel(cmd: str | None = None):
    cancelled = cancel_pending(cmd)
    return {"status": "cancelled" if cancelled else "nothing_pending"}


# --- Display / media / clipboard commands ---

@command("monitor", "Turn the monitor on or off.", primary=True)
def monitor(on: bool = False):
    HWND_BROADCAST = 0xFFFF
    WM_SYSCOMMAND = 0x0112
    SC_MONITORPOWER = 0xF170
    ctypes.windll.user32.SendMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER,
                                      -1 if on else 2)
    return {"status": "monitor_on" if on else "monitor_off"}


def _capture_screen_b64() -> str:
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$b = New-Object System.Drawing.Bitmap("
        "[System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Width, "
        "[System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Height);"
        "$g = [System.Drawing.Graphics]::FromImage($b);"
        "$g.CopyFromScreen(0,0,0,0,$b.Size);"
        "$ms = New-Object System.IO.MemoryStream;"
        "$b.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png);"
        "[Convert]::ToBase64String($ms.ToArray())"
    )
    out = subprocess.run([POWERSHELL, "-NoProfile", "-Command", ps],
                         capture_output=True, text=True, check=True,
                         **_SUBPROC_KWARGS)
    return out.stdout.strip()


@command("screenshot", "Capture the screen and return the image.", primary=True)
def screenshot():
    return {"image": _capture_screen_b64()}


@command("copy", "Read the PC clipboard and return its text.", tab="tools")
def copy():
    out = subprocess.run([POWERSHELL, "-NoProfile", "-Command", "Get-Clipboard"],
                         capture_output=True, text=True, check=True,
                         **_SUBPROC_KWARGS)
    return {"text": out.stdout.rstrip("\n")}


@command("paste", "Write text to the PC clipboard.", tab="tools")
def paste(text: str = ""):
    import tempfile
    path = tempfile.mktemp(suffix=".txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        subprocess.run([POWERSHELL, "-NoProfile", "-Command",
                        f"Get-Content -Raw -Path '{path}' | Set-Clipboard"], check=True,
                        **_SUBPROC_KWARGS)
    finally:
        os.remove(path)
    return {"status": "copied", "length": len(text)}


@command("brightness", "Set monitor brightness (0-100).", range=["level"], tab="media")
def brightness(level: int = 50):
    level = max(0, min(100, int(level)))
    # Snap to a level the monitor actually supports (if discoverable).
    ps = (
        "$near = {level}; "
        "try { "
        "$b = Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightness "
        "-ErrorAction Stop | Select-Object -First 1; "
        "if ($b -and $b.Level) { "
        "$near = ($b.Level | Sort-Object {[math]::Abs($_ - {level})} | "
        "Select-Object -First 1) "
        "} "
        "} catch { } "
        "Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightnessMethods "
        "-ErrorAction Stop | ForEach-Object { $_.WmiSetBrightness(1, $near) }"
    )
    try:
        subprocess.run([POWERSHELL, "-NoProfile", "-Command", ps], check=True,
                       capture_output=True, text=True, **_SUBPROC_KWARGS)
    except subprocess.CalledProcessError as exc:
        msg = (exc.stderr or exc.stdout or str(exc)).strip().splitlines()
        msg = msg[-1] if msg else "failed"
        return {"status": "error", "detail": msg,
                "hint": "monitor brightness may not be supported on this display"}
    return {"status": "set", "level": level}


@command("volume", "Set system volume (0-100).", range=["level"], tab="media")
def volume(level: int = 50):
    level = max(0, min(100, int(level)))
    _set_master_volume(level)
    return {"status": "set", "volume": level}


@command("play", "Toggle media play/pause.", tab="media")
def play():
    VK_MEDIA_PLAY_PAUSE = 0xB3
    ctypes.windll.user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, 0, 0)
    ctypes.windll.user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, 2, 0)
    return {"status": "toggled"}


@command("bluetooth", "Enable or disable the Bluetooth radio (needs admin).", tab="tools")
def bluetooth(on: bool = True):
    verb = "Enable" if on else "Disable"
    ps = (f"$e = Get-PnpDevice -Class Bluetooth -ErrorAction Stop | "
          f"{verb}-PnpDevice -Confirm:$false -ErrorAction Stop; "
          f"Write-Output 'ok'")
    try:
        out = subprocess.run([POWERSHELL, "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, check=True,
                             **_SUBPROC_KWARGS)
    except subprocess.CalledProcessError as exc:
        msg = (exc.stderr or exc.stdout or str(exc)).strip().splitlines()
        msg = msg[-1] if msg else "failed"
        return {"status": "error", "detail": msg,
                "hint": "run the server as Administrator"}
    return {"status": "bluetooth_" + ("on" if on else "off")}


@command("wifi", "Enable or disable the Wi-Fi interface (needs admin).", tab="tools")
def wifi(on: bool = True):
    state = "enable" if on else "disable"
    ps = (f"netsh interface set interface name='Wi-Fi' admin={state}; "
          f"if ($LASTEXITCODE -eq 0) {{ Write-Output 'ok' }} "
          f"else {{ Write-Error 'netsh failed (admin required?)' }}")
    try:
        subprocess.run([POWERSHELL, "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, check=True,
                       **_SUBPROC_KWARGS)
    except subprocess.CalledProcessError as exc:
        msg = (exc.stderr or exc.stdout or str(exc)).strip().splitlines()
        msg = msg[-1] if msg else "failed"
        return {"status": "error", "detail": msg,
                "hint": "run the server as Administrator"}
    return {"status": "wifi_" + ("on" if on else "off")}


def _system_stats():
    stats = {}
    try:
        import socket
        stats["hostname"] = socket.gethostname()
    except Exception:
        pass
    return stats


@command("ping", "Measure round-trip latency to the server.", ping=True, tab="tools")
def ping():
    return {"pong": True}


@command("status", "Return basic server/PC status.", tab="tools")
def status():
    stats = _system_stats()
    return {
        "status": "ok",
        "uptime_s": int(time.time() - START_TIME),
        "requests": REQUEST_COUNT,
        "last_command": LAST_COMMAND,
        "pending": list(pending.keys()),
        "commands": list(commands.keys()),
        **stats,
    }


@command("list", "List all available commands.", tab="tools")
def list_commands():
    return {
        name: {
            "description": meta["description"],
            "params": [{"name": p["name"], "type": p["type"]} for p in meta["params"]],
        }
        for name, meta in commands.items()
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "PCRemote/1.0"
    protocol_version = "HTTP/1.1"  # keep-alive: reuse one connection for all pings

    def _send(self, code: int, payload):
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, query: dict) -> bool:
        if not TOKEN:
            return True
        if query.get("token", [None])[0] == TOKEN:
            return True
        self._send(401, {"error": "unauthorized", "hint": "add ?token=YOUR_TOKEN"})
        return False

    def _parse(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        route = parsed.path.rstrip("/") or "/"
        return route, query

    def _run(self, cmd_name: str, query: dict, body: dict | None = None):
        if not self._authorized(query):
            return
        entry = commands.get(cmd_name.lower())
        if not entry:
            self._send(404, {"error": f"unknown command: {cmd_name}",
                             "available": list(commands.keys())})
            return
        kwargs: dict = {}
        for p in entry["params"]:
            pname = p["name"]
            if pname in query:
                kwargs[pname] = _coerce(query[pname][0], p["type"])
            elif body and pname in body:
                kwargs[pname] = _coerce(str(body[pname]), p["type"])
        try:
            result = entry["func"](**kwargs)
            _set_last(cmd_name)
        except Exception as exc:
            self._send(500, {"error": str(exc)})
            return
        self._send(200, {"command": cmd_name, "result": result})

    def do_GET(self):
        _record_request()
        route, query = self._parse()
        if route == "/":
            return self._serve_index(query)
        if route == "/api/commands":
            if not self._authorized(query):
                return
            return self._send(200, list_commands())
        return self._run(route.lstrip("/"), query)

    def do_POST(self):
        _record_request()
        route, query = self._parse()
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        return self._run(route.lstrip("/"), query, body)

    def _serve_index(self, query: dict):
        if not self._authorized(query):
            return
        token = query.get("token", [TOKEN])[0]
        visible = {n: m for n, m in commands.items() if not m.get("hide")}
        primary = [self._command_card(n, m) for n, m in visible.items() if m.get("primary")]
        tabs: dict[str, list] = {}
        for n, m in visible.items():
            if m.get("primary"):
                continue
            tabs.setdefault(m.get("tab") or "tools", []).append(self._command_card(n, m))
        tab_order = [t for t in ("media", "tools", "power") if t in tabs]
        tab_sections = ""
        for t in tab_order:
            tab_sections += (
                f'<div class="other-btn" onclick="this.nextElementSibling'
                f'.classList.toggle(\'open\'); this.querySelector(\'chev\')'
                f'.classList.toggle(\'open\')">{t.title()}'
                f'<span class="chev"></span></div>'
                f'<div class="other">' + "".join(tabs[t]) + '</div>'
            )
        cards = "".join(primary) + tab_sections
        html = f"""<!doctype html><html><head><meta name="viewport"
        content="width=device-width,initial-scale=1"><title>PC Remote</title>
        <meta name="color-scheme" content="dark"><style>
        :root{{color-scheme:dark}}
        *{{box-sizing:border-box}}
        body{{font-family:system-ui;max-width:640px;margin:2rem auto;
        padding:0 1rem;background:#0f1115;color:#e6e6e6}}
        h1{{font-size:1.4rem;color:#fff;margin-bottom:1.2rem}}
        .card{{border:1px solid #2a2f3a;border-radius:10px;margin:.5rem 0;
        background:#171a21;overflow:hidden}}
        .row{{display:flex;align-items:center;gap:.6rem;padding:.7rem .9rem;
        cursor:pointer;user-select:none}}
        .row:hover{{background:#1d212b}}
        .name{{font-weight:600;color:#4da3ff;font-size:1.05rem;flex:1}}
        .chev-wrap{{display:none;align-items:center;justify-content:center;
        width:40px;height:40px;margin:-8px -8px -8px 0;border-radius:8px;
        flex:none;cursor:pointer}}
        .chev-wrap.show{{display:flex}}
        .chev-wrap:hover{{background:#222732}}
        .chev{{width:8px;height:8px;border-right:2px solid #9aa4b2;
        border-bottom:2px solid #9aa4b2;transform:rotate(-45deg);
        transition:transform .18s ease}}
        .chev.open{{transform:rotate(45deg)}}
        .details{{display:none;flex-direction:column;gap:.5rem;
        padding:0 .9rem .8rem}}
        .details.open{{display:flex}}
        .details label{{display:flex;flex-direction:column;font-size:.8rem;
        color:#9aa4b2;gap:.2rem}}
        .details input{{background:#0f1115;border:1px solid #2a2f3a;color:#e6e6e6;
        border-radius:6px;padding:.4rem .5rem;font-size:.9rem}}
        .out{{padding:.5rem .6rem;border-radius:6px;background:#0f1115;
        border:1px solid #2a2f3a;font-family:ui-monospace,monospace;
        font-size:.8rem;white-space:pre-wrap;display:none;color:#a7f3d0}}
        .out.err{{color:#fca5a5}}
        .out.show{{display:block}}
        .confirm{{display:none;align-items:center;gap:.6rem;margin:.2rem .9rem .8rem;
        padding:.5rem .7rem;border-radius:8px;background:#2a1414;
        border:1px solid #5b2b2b;font-size:.85rem;color:#fca5a5}}
        .confirm.show{{display:flex}}
        .confirm button{{background:#dc2626;color:#fff;border:0;padding:.4rem .8rem;
        border-radius:6px;font-weight:600;cursor:pointer}}
        .confirm button.no{{background:#2a2f3a;color:#e6e6e6}}
        .other-btn{{display:flex;align-items:center;gap:.5rem;margin-top:2rem;
        padding:.6rem .9rem;border:1px solid #2a2f3a;border-radius:10px;
        background:#171a21;cursor:pointer;user-select:none;color:#9aa4b2;
        font-weight:600}}
        .other-btn:hover{{background:#1d212b}}
        .other-btn .chev{{width:8px;height:8px;border-right:2px solid #9aa4b2;
        border-bottom:2px solid #9aa4b2;transform:rotate(-45deg);
        transition:transform .18s ease;margin-left:auto}}
        .other-btn .chev.open{{transform:rotate(45deg)}}
        .other{{display:none;flex-direction:column;margin-top:.25rem}}
        .other.open{{display:flex}}
        .other .card{{margin:.25rem 0}}
        .undo{{display:none;margin:.4rem .9rem .8rem}}
        .undo.show{{display:block}}
        .undo button{{background:#2a2f3a;color:#e6e6e6;border:1px solid #3a4150;
        padding:.4rem .8rem;border-radius:6px;font-weight:600;cursor:pointer;
        font-size:.85rem}}
        .undo button:hover{{background:#222732}}
        .out img{{max-width:100%;border-radius:6px;display:block}}
        .slider{{display:flex;align-items:center;gap:.7rem;margin:.15rem 0}}
        .slider input[type=range]{{flex:1;width:100%;height:6px;-webkit-appearance:none;
        appearance:none;background:#2a2f3a;border-radius:3px;outline:none;cursor:pointer}}
        .slider input[type=range]::-webkit-slider-thumb{{-webkit-appearance:none;
        appearance:none;width:18px;height:18px;border-radius:50%;
        background:#4da3ff;cursor:pointer;border:2px solid #0f1115}}
        .slider input[type=range]::-moz-range-thumb{{width:18px;height:18px;
        border-radius:50%;background:#4da3ff;cursor:pointer;border:2px solid #0f1115}}
        .slider .val{{min-width:2.5rem;text-align:right;color:#e6e6e6;font-size:.85rem;
        font-variant-numeric:tabular-nums}}
        </style></head>
        <body><h1>PC Remote Control</h1>{cards}
        <script>
        const TOKEN = {json.dumps(token)};
        function toggleDetails(card) {{
          card.querySelector('.details').classList.toggle('open');
          card.querySelector('.chev').classList.toggle('open');
        }}
        function fmtMs(ms) {{
          let s = ms.toFixed(2);
          if (s.endsWith('.00')) return s.slice(0, -3);
          if (s.endsWith('0')) return s.slice(0, -1);
          return s;
        }}
        async function measureLatency(cmd) {{
          const url = '/' + cmd + '?token=' + encodeURIComponent(TOKEN);
          const opts = {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:'{{}}'}};
          await fetch(url, opts);  // warm up the connection (skip handshake cost)
          const t = performance.now();
          await fetch(url, opts);
          return performance.now() - t;
        }}
        async function run(card, cmd) {{
          const out = card.querySelector('.out');
          const inputs = card.querySelectorAll('.details input');
          const body = {{}};
          inputs.forEach(i => {{ if (i.value !== '') body[i.name] = i.value; }});
          const isPing = card.querySelector('.ping-flag') !== null;
          let data = null;
          let ok = true;
          try {{
            if (isPing) {{
              const ms = await measureLatency(cmd);
              out.textContent = fmtMs(ms) + 'ms';
            }} else {{
              const res = await fetch('/' + cmd + '?token=' + encodeURIComponent(TOKEN),
                {{method:'POST', headers:{{'Content-Type':'application/json'}},
                 body: JSON.stringify(body)}});
              data = await res.json();
              ok = res.ok;
              if (data.result && data.result.image) {{
                out.textContent = '';
                const img = document.createElement('img');
                img.src = 'data:image/png;base64,' + data.result.image;
                out.appendChild(img);
              }} else if (data.result && 'text' in data.result) {{
                out.textContent = data.result.text || '(empty clipboard)';
              }} else {{
                out.textContent = JSON.stringify(data, null, 2);
              }}
            }}
          }} catch (e) {{
            ok = false;
            out.textContent = String(e);
          }}
          out.className = 'out show' + (ok ? '' : ' err');
          card.querySelector('.details').classList.add('open');
          const cw = card.querySelector('.chev-wrap'); if (cw) cw.classList.add('show');
          const ch = card.querySelector('.chev'); if (ch) ch.classList.add('open');
          const u = card.querySelector('.undo');
          if (u && data && data.result && data.result.seconds > 0) u.classList.add('show');
        }}
        function onRow(card, cmd, needsConfirm, ev) {{
          if (needsConfirm) {{
            card.querySelector('.confirm').classList.add('show');
            return;
          }}
          run(card, cmd);
        }}
        function doConfirm(card, cmd, yes) {{
          card.querySelector('.confirm').classList.remove('show');
          if (yes) run(card, cmd);
        }}
        async function doUndo(card, cmd) {{
          const out = card.querySelector('.out');
          try {{
            const res = await fetch('/cancel?token=' + encodeURIComponent(TOKEN),
              {{method:'POST', headers:{{'Content-Type':'application/json'}},
               body: JSON.stringify({{cmd: cmd}})}});
            const data = await res.json();
            out.textContent = JSON.stringify(data, null, 2);
            out.className = 'out show' + (res.ok ? '' : ' err');
          }} catch (e) {{
            out.textContent = String(e); out.className = 'out show err';
          }}
          card.querySelector('.undo').classList.remove('show');
        }}
        async function syncPending() {{
          try {{
            const res = await fetch('/status?token=' + encodeURIComponent(TOKEN),
              {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:'{{}}'}});
            const data = await res.json();
            (data.result.pending || []).forEach(cmd => {{
              const card = document.querySelector('.card[data-cmd="' + cmd + '"] .row');
              if (card) {{
                const c = card.closest('.card');
                c.querySelector('.undo').classList.add('show');
                c.querySelector('.details').classList.add('open');
                const cw = c.querySelector('.chev-wrap'); if (cw) cw.classList.add('show');
                const ch = c.querySelector('.chev'); if (ch) ch.classList.add('open');
              }}
            }});
          }} catch (e) {{}}
        }}
        syncPending();
        </script></body></html>"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _command_card(self, name: str, meta: dict) -> str:
        params = meta["params"]
        needs_confirm = meta.get("confirm", False)
        rng = meta.get("range", set())
        fields = "".join(
            (f'<div class="slider"><input name="{p["name"]}" type="range" '
             f'min="0" max="100" value="{p["default"] if p["has_default"] else 50}" '
             f'oninput="this.nextElementSibling.textContent=this.value">'
             f'<span class="val">{p["default"] if p["has_default"] else 50}</span></div>'
             if p["name"] in rng else
             f'<label>{p["name"]} ({p["type"]})'
             f'<input name="{p["name"]}" type="'
             f'{"number" if p["type"] in ("int","float") else "text"}"'
             f' placeholder="{p["default"] if p["has_default"] else ""}"></label>')
            for p in params
        )
        confirm_box = (f'<div class="confirm">Are you sure? '
                       f'<button onclick="doConfirm(this.closest(\'.card\'), '
                       f'\'{name}\', true)">Yes</button>'
                       f'<button class="no" onclick="doConfirm(this.closest'
                       f'(\'.card\'), \'{name}\', false)">No</button></div>'
                       ) if needs_confirm else ""
        chev_cls = "chev-wrap show" if params else "chev-wrap"
        chev = (f'<span class="{chev_cls}" onclick="event.stopPropagation();'
                'toggleDetails(this.closest(\'.card\'))">'
                '<span class="chev" title="details"></span></span>')
        undo_box = (f'<div class="undo"><button onclick="doUndo('
                    f'this.closest(\'.card\'), \'{name}\')">Cancel</button></div>'
                    ) if meta.get("undo") else ""
        ping_flag = '<span class="ping-flag" style="display:none"></span>' if meta.get("ping") else ""
        details = f'<div class="details">{fields}<div class="out"></div></div>'
        return (f'<div class="card" data-cmd="{name}"><div class="row" '
                f'onclick="onRow(this.closest(\'.card\'), \'{name}\', '
                f'{str(needs_confirm).lower()}, event)">'
                f'<span class="name">{name}</span>{chev}{ping_flag}'
                f'</div>{details}{confirm_box}{undo_box}</div>')

    def log_message(self, *args):
        pass


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"PC Remote server listening on http://{HOST}:{PORT}")
    if not TOKEN:
        print("WARNING: no PC_API_TOKEN set - server is open on your network!")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
    