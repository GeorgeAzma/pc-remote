"""Command registry + built-in PC actions.

Every action is a function decorated with @command; it becomes an HTTP
endpoint (GET/POST /<name>) and a control in the web UI. Parameters come
from the query string or JSON body and are coerced by type annotation.
"""
import asyncio
import ctypes
import glob
import inspect
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable

import win32

CREATE_NO_WINDOW = 0x08000000
START_TIME = time.time()
commands: dict[str, dict] = {}


def command(name: str | None = None, description: str = "", confirm: bool = False,
            primary: bool = False, undo: bool = False, hide: bool = False, ping: bool = False,
            range: list[str] | None = None, tab: str = "", live: bool = False,
            icon: str = "", title: str = "", state: Callable | None = None,
            danger: bool = False) -> Callable:
    """Register a function as a command.

    confirm  ask before running;           danger   red styling in the UI
    undo     result can be cancelled;      hide     API only, not in the UI
    primary  pinned to the top;            ping     UI shows round-trip latency
    range    int params drawn as 0-100 sliders
    tab      UI group: 'media' | 'tools' | 'power'
    live     fire on every change (slider drag / toggle) without a tap
    icon     icon name for the UI;          title   display label
    state    zero-arg callable returning current values for the UI
    """
    def decorator(func):
        params = [{"name": n, "type": _type_name(p.annotation),
                   "default": None if p.default is inspect.Parameter.empty else p.default,
                   "has_default": p.default is not inspect.Parameter.empty}
                  for n, p in inspect.signature(func).parameters.items()]
        cmd = (name or func.__name__).lower()
        commands[cmd] = {"func": func, "description": description or (func.__doc__ or "").strip(),
                         "params": params, "confirm": confirm, "primary": primary, "undo": undo,
                         "hide": hide, "ping": ping, "range": set(range or []), "tab": tab,
                         "live": live, "icon": icon, "title": title or cmd.title(),
                         "state": state, "danger": danger}
        return func
    return decorator


def _type_name(annotation) -> str:
    return {bool: "bool", int: "int", float: "float"}.get(annotation, "str")


def coerce(value, type_name: str):
    if type_name == "bool":
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on", "y")
    if type_name == "int":
        return int(float(value))
    if type_name == "float":
        return float(value)
    return str(value)


def run(name: str, args: dict):
    entry = commands[name]
    kwargs = {p["name"]: coerce(args[p["name"]], p["type"]) for p in entry["params"] if p["name"] in args}
    return entry["func"](**kwargs)


def describe() -> dict:
    """Command metadata for the UI (and /api/commands)."""
    return {name: {"description": m["description"], "title": m["title"], "icon": m["icon"],
                   "tab": m["tab"], "confirm": m["confirm"], "primary": m["primary"],
                   "undo": m["undo"], "hide": m["hide"], "ping": m["ping"], "live": m["live"],
                   "danger": m["danger"], "range": sorted(m["range"]), "stateful": bool(m["state"]),
                   "params": [{k: p[k] for k in ("name", "type", "default")} for p in m["params"]]}
            for name, m in commands.items()}


def states(names: list[str] | None = None) -> dict:
    """Current values for stateful commands, fetched in parallel (some,
    like radio state, take a moment)."""
    todo = {n: m["state"] for n, m in commands.items() if m["state"] and (not names or n in names)}
    out, threads = {}, []

    def get(n, fn):
        try:
            out[n] = fn()
        except Exception as exc:  # noqa: BLE001 - a broken getter must not break the UI
            out[n] = {"error": str(exc)}
    for n, fn in todo.items():
        t = threading.Thread(target=get, args=(n, fn), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(5)
    return out


# --- pending (cancellable) actions ---------------------------------------
pending: dict[str, dict] = {}
_pending_lock = threading.Lock()


def _register_pending(name: str, seconds: int, cancel_fn: Callable[[], None]):
    with _pending_lock:
        pending[name] = {"cancel": cancel_fn, "at": time.time() + seconds}


def _clear_pending(name: str):
    with _pending_lock:
        pending.pop(name, None)


def pending_info() -> dict:
    with _pending_lock:
        return {n: round(p["at"]) for n, p in pending.items()}


# --- stats ---------------------------------------------------------------
REQUEST_COUNT = 0
LAST_COMMAND = None


def record(name: str | None = None):
    global REQUEST_COUNT, LAST_COMMAND
    REQUEST_COUNT += 1
    if name:
        LAST_COMMAND = name


# ==========================================================================
# Power
# ==========================================================================
def _timed(name: str, seconds: int, action: Callable[[], None], cancel=None):
    """Run now, or after `seconds` with a Cancel option."""
    if seconds <= 0:
        action()
        return {"status": name, "seconds": 0}
    if cancel is None:
        timer = threading.Timer(seconds, lambda: (_clear_pending(name), action()))
        timer.daemon = True
        timer.start()
        cancel = timer.cancel
    else:
        threading.Timer(seconds, lambda: _clear_pending(name)).start()
    _register_pending(name, seconds, cancel)
    return {"status": f"{name}_scheduled", "seconds": seconds, "at": round(time.time() + seconds)}


@command("sleep", "Put the PC to sleep.", confirm=True, primary=True, undo=True, icon="moon", tab="power")
def sleep(seconds: int = 0):
    return _timed("sleep", seconds, lambda: ctypes.windll.powrprof.SetSuspendState(0, 0, 0))


@command("lock", "Lock the workstation.", icon="lock", tab="power", primary=True)
def lock():
    ctypes.windll.user32.LockWorkStation()
    return {"status": "locked"}


@command("monitor", "Turn the display off (any input wakes it) or back on.", icon="display",
         title="Screen off", tab="power", primary=True)
def monitor(on: bool = False):
    if on:
        # SC_MONITORPOWER(-1) alone is unreliable once fully off; a synthetic
        # zero move plus ES_DISPLAY_REQUIRED guarantees the wake.
        ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, -1)
        ctypes.windll.kernel32.SetThreadExecutionState(0x00000002)
        ctypes.windll.user32.mouse_event(0x0001, 0, 1, 0, 0)
        ctypes.windll.user32.mouse_event(0x0001, 0, -1, 0, 0)
    else:
        # PostMessage: SendMessage to HWND_BROADCAST blocks on hung windows.
        ctypes.windll.user32.PostMessageW(0xFFFF, 0x0112, 0xF170, 2)
    return {"status": "monitor_on" if on else "monitor_off"}


@command("hibernate", "Hibernate the PC.", confirm=True, icon="snow", tab="power")
def hibernate():
    ctypes.windll.powrprof.SetSuspendState(1, 0, 0)
    return {"status": "hibernating"}


def _shutdown_cmd(flag: str, force: bool, seconds: int, name: str):
    subprocess.run(["shutdown", flag, *(["/f"] if force else []), "/t", str(max(0, seconds))],
                   check=True, creationflags=CREATE_NO_WINDOW)
    abort = lambda: subprocess.run(["shutdown", "/a"], creationflags=CREATE_NO_WINDOW)  # noqa: E731
    return _timed(name, seconds, lambda: None, cancel=abort) if seconds else {"status": name, "seconds": 0}


@command("restart", "Restart the PC.", confirm=True, undo=True, icon="restart", tab="power", danger=True)
def restart(force: bool = False, seconds: int = 0):
    return _shutdown_cmd("/r", force, seconds, "restart")


@command("shutdown", "Shut the PC down.", confirm=True, undo=True, icon="power", tab="power", danger=True)
def shutdown(force: bool = False, seconds: int = 0):
    return _shutdown_cmd("/s", force, seconds, "shutdown")


@command("signout", "Sign out of Windows.", confirm=True, icon="logout", title="Sign out", tab="power", danger=True)
def signout():
    subprocess.run(["shutdown", "/l"], creationflags=CREATE_NO_WINDOW)
    return {"status": "signing_out"}


@command("cancel", "Cancel a pending sleep/shutdown/restart.", hide=True)
def cancel(cmd: str = ""):
    with _pending_lock:
        names = [cmd] if cmd else list(pending)
        entries = [pending.pop(n) for n in names if n in pending]
    for e in entries:
        try:
            e["cancel"]()
        except Exception:  # noqa: BLE001
            pass
    return {"status": "cancelled" if entries else "nothing_pending"}


# ==========================================================================
# Media
# ==========================================================================
def _vol_state():
    return win32.VOLUME.get()


@command("volume", "Set the system volume.", range=["level"], tab="media", live=True, icon="volume",
         state=_vol_state)
def volume(level: int = 50):
    win32.VOLUME.set(max(0, min(100, level)))
    return {"status": "set", "level": level}


@command("mute", "Mute or unmute the system audio.", tab="media", live=True, icon="mute",
         state=lambda: {"on": win32.VOLUME.get()["muted"]})
def mute(on: bool = True):
    win32.VOLUME.set(("mute", on))
    return {"status": "muted" if on else "unmuted"}


@command("brightness", "Set the monitor brightness (DDC/CI).", range=["level"], tab="media", live=True,
         icon="sun", state=lambda: {"level": win32.BRIGHTNESS.get()})
def brightness(level: int = 50):
    win32.BRIGHTNESS.set(max(0, min(100, level)))
    return {"status": "set", "level": level}


def _media_key(vk):
    win32.key(vk, True)
    win32.key(vk, False)


@command("play", "Play / pause media.", tab="media", icon="playpause", title="Play/Pause")
def play():
    _media_key(0xB3)
    return {"status": "toggled"}


@command("next", "Next track.", tab="media", icon="next")
def next_track():
    _media_key(0xB0)
    return {"status": "next"}


@command("prev", "Previous track.", tab="media", icon="prev", title="Previous")
def prev_track():
    _media_key(0xB1)
    return {"status": "prev"}


# ==========================================================================
# Radios (Windows.Devices.Radios: the Action Center toggles, no admin)
# ==========================================================================
def _radio(kind: int, on: bool | None = None):
    import winrt.windows.foundation.collections  # noqa: F401 - needed by get_radios_async
    import winrt.windows.devices.radios as radios

    async def go():
        for r in await radios.Radio.get_radios_async():
            if int(r.kind) == kind:
                if on is not None:
                    await r.set_state_async(radios.RadioState.ON if on else radios.RadioState.OFF)
                return int(r.state) == 1
        raise RuntimeError("radio not found")
    return asyncio.run(go())


def _netsh_wifi(on: bool):
    subprocess.run(["netsh", "interface", "set", "interface", "name=Wi-Fi",
                    f"admin={'enable' if on else 'disable'}"], check=True,
                   capture_output=True, creationflags=CREATE_NO_WINDOW)


@command("wifi", "Turn Wi-Fi on or off.", tab="tools", live=True, icon="wifi", title="Wi-Fi",
         state=lambda: {"on": _radio(1)})
def wifi(on: bool = True):
    try:
        return {"status": "wifi_on" if _radio(1, on) else "wifi_off"}
    except Exception:  # noqa: BLE001 - fall back to the adapter (needs admin)
        _netsh_wifi(on)
        return {"status": "wifi_" + ("on" if on else "off")}


@command("bluetooth", "Turn Bluetooth on or off.", tab="tools", live=True, icon="bluetooth",
         state=lambda: {"on": _radio(3)})
def bluetooth(on: bool = True):
    return {"status": "bluetooth_on" if _radio(3, on) else "bluetooth_off"}


# ==========================================================================
# Tools
# ==========================================================================
@command("screenshot", "Capture the screen.", primary=True, icon="camera", tab="tools")
def screenshot():
    import base64
    return {"image": base64.b64encode(win32.screenshot_png()).decode()}


@command("copy", "Read the PC clipboard.", tab="tools", hide=True)
def copy():
    return {"text": win32.get_clipboard()}


@command("paste", "Write text to the PC clipboard.", tab="tools", hide=True)
def paste(text: str = ""):
    win32.set_clipboard(text)
    return {"status": "copied", "length": len(text)}


@command("sendlink", "Open a link in the PC's browser.", tab="tools", icon="link", title="Open link")
def sendlink(url: str = ""):
    u = url.strip()
    if not u:
        raise ValueError("no url")
    if "://" not in u:
        u = "https://" + u
    webbrowser.open(u)
    return {"status": "opened", "url": u}


def _start_menu_apps() -> dict[str, str]:
    roots = [os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), r"Microsoft\Windows\Start Menu\Programs"),
             os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs")]
    apps = {}
    for root in roots:
        for path in glob.glob(os.path.join(root, "**", "*.lnk"), recursive=True):
            name = os.path.splitext(os.path.basename(path))[0]
            low = name.lower()
            if any(w in low for w in ("uninstall", "readme", "help", "documentation", "release notes")):
                continue
            apps.setdefault(name, path)
    return apps


@command("apps", "List launchable apps (Start menu).", hide=True)
def apps():
    return {"apps": sorted(_start_menu_apps(), key=str.lower)}


@command("launch", "Open an app, file, folder or URL on the PC.", tab="tools", icon="rocket", title="Open app")
def launch(target: str = ""):
    t = target.strip().strip('"')
    if not t:
        raise ValueError("nothing to open")
    found = _start_menu_apps()
    match = found.get(t) or next((p for n, p in found.items() if n.lower() == t.lower()), None) \
        or next((p for n, p in sorted(found.items(), key=lambda kv: len(kv[0])) if t.lower() in n.lower()), None)
    os.startfile(match or os.path.expandvars(os.path.expanduser(t)))
    return {"status": "opened", "target": os.path.splitext(os.path.basename(match))[0] if match else t}


@command("processes", "Top processes by memory.", hide=True)
def processes(limit: int = 25):
    out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True,
                         creationflags=CREATE_NO_WINDOW, encoding="utf-8", errors="replace").stdout
    groups: dict[str, dict] = {}
    for line in out.splitlines():
        cols = [c.strip('"') for c in line.split('","')]
        if len(cols) < 5:
            continue
        name, pid = cols[0], int(cols[1])
        mem = int("".join(ch for ch in cols[4] if ch.isdigit()) or 0) * 1024
        if cols[2].lower() == "services" and name.lower() in ("svchost.exe", "system", "registry"):
            continue
        g = groups.setdefault(name.lower(), {"name": name, "pids": [], "mem": 0})
        g["pids"].append(pid)
        g["mem"] += mem
    skip = {"system idle process", "system", "registry", "memory compression", "secure system",
            "smss.exe", "csrss.exe", "wininit.exe", "services.exe", "lsass.exe", "svchost.exe",
            "fontdrvhost.exe", "winlogon.exe", "dwm.exe", "pythonw.exe", "python.exe", "ffmpeg.exe",
            "conhost.exe", "openconsole.exe"}
    rows = sorted((g for k, g in groups.items() if k not in skip), key=lambda g: -g["mem"])
    return {"processes": rows[:max(1, limit)]}


@command("kill", "End a process by name (all instances) or pid.", hide=True, confirm=True)
def kill(name: str = "", pid: int = 0):
    args = ["taskkill", "/f", "/t"] + (["/pid", str(pid)] if pid else ["/im", name])
    r = subprocess.run(args, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or "taskkill failed")
    return {"status": "killed", "target": pid or name}


@command("sendfile", "Upload files from this device to the PC's Downloads folder.", tab="tools",
         icon="upload", title="Send file")
def sendfile():
    return {"status": "ready"}  # the upload itself is POST /upload


@command("ping", "Round-trip latency to the PC.", ping=True, tab="tools", icon="pulse", hide=True)
def ping():
    return {"pong": True}


@command("status", "Server and PC status.", tab="tools", icon="info", hide=True)
def status():
    return {"status": "ok", "hostname": socket.gethostname(), "uptime_s": int(time.time() - START_TIME),
            "requests": REQUEST_COUNT, "last_command": LAST_COMMAND, "pending": pending_info(),
            **win32.STATS.snapshot()}


_STARTED = time.time()
_about_cache: dict = {}


def _reg(path, name):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def _windows() -> str:
    cv = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    build = sys.getwindowsversion().build
    # ProductName still says "Windows 10" on Windows 11
    name = (_reg(cv, "ProductName") or "Windows").replace("Windows 10", "Windows 11" if build >= 22000 else "Windows 10")
    rel, ubr = _reg(cv, "DisplayVersion"), _reg(cv, "UBR")
    return " ".join(x for x in (name, rel, f"(build {build}{f'.{ubr}' if ubr else ''})") if x)


def _gpus() -> list[str]:
    cls = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    out = []
    for i in range(10):
        d = _reg(rf"{cls}\{i:04d}", "DriverDesc")
        if d and "Basic" not in d and d not in out:
            out.append(d)
    return out


def _server_version():
    """(short commit, its time) the server runs from, read from .git (no git needed)."""
    g = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".git")
    try:
        head = open(os.path.join(g, "HEAD")).read().strip()
        ref = head[5:] if head.startswith("ref: ") else None
        if ref and os.path.exists(os.path.join(g, ref)):
            return open(os.path.join(g, ref)).read().strip()[:7], os.path.getmtime(os.path.join(g, ref))
        if ref:
            sha = next(ln.split()[0] for ln in open(os.path.join(g, "packed-refs")) if ln.strip().endswith(ref))
            return sha[:7], None
        return head[:7], None
    except (OSError, StopIteration):
        return None, None


def _ffmpeg_version():
    import video
    if not video.FFMPEG:
        return None
    try:
        first = subprocess.run([video.FFMPEG, "-version"], capture_output=True, text=True, timeout=10,
                               creationflags=0x08000000).stdout.splitlines()[0]
        return first.split(" version ", 1)[1].split(" ")[0] if " version " in first else first
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def _certificate():
    """The local CA the HTTPS certificate is signed with, for the install guide."""
    import certs
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        with open(certs.CA_CERT, "rb") as f:
            c = x509.load_pem_x509_certificate(f.read())
        fp = c.fingerprint(hashes.SHA256()).hex().upper()
        return {"sha256": ":".join(fp[i:i + 2] for i in range(0, len(fp), 2)),
                "expires": c.not_valid_after_utc.timestamp(),
                "name": c.subject.rfc4514_string()}
    except Exception:  # noqa: BLE001 - no cryptography, or no certificate yet
        return None


@command("about", "PC, server and connection details.", hide=True)
def about():
    import certs
    import tiles
    import video
    if not _about_cache:  # what doesn't change while the server runs
        sha, when = _server_version()
        _about_cache.update(
            windows=_windows(), cores=os.cpu_count(), gpus=_gpus(), version=sha, version_time=when,
            cpu=(_reg(r"HARDWARE\DESCRIPTION\System\CentralProcessor\0", "ProcessorNameString") or "").strip() or None,
            python=sys.version.split()[0], ffmpeg=_ffmpeg_version())
    st = win32.STATS.snapshot()
    vs = video.status()
    return {**_about_cache, "host": socket.gethostname(), "ram_gb": st.get("ram_total_gb"),
            "uptime_s": st.get("uptime_s"), "server_up_s": round(time.time() - _STARTED),
            "displays": [{k: d[k] for k in ("name", "w", "h", "hz", "primary")} for d in win32.displays()],
            "ips": [ip for ip in certs.local_ips() if ip != "127.0.0.1"],
            "encoders": {"h264": vs.get("h264"), "hevc": vs.get("hevc")}, "tiles": tiles.available(),
            "token": bool(os.environ.get("PC_API_TOKEN")), "certificate": _certificate()}


@command("stats", "Live CPU / RAM / GPU usage.", hide=True)
def stats():
    return win32.STATS.snapshot()


@command("list", "List all commands.", hide=True)
def list_commands():
    return {n: {"description": m["description"], "params": [{"name": p["name"], "type": p["type"]}
                                                           for p in m["params"]]} for n, m in commands.items()}


# --- API-compatible input endpoints (the UI uses the /ws socket instead) ---
@command("type", "Type text into the focused window.", hide=True)
def type_text(text: str = ""):
    win32.type_text(text)
    return {"status": "typed", "length": len(text)}


@command("keys", "Send a key combination (ctrl+c, alt+tab, win+d, f5 ...).", hide=True)
def keys(combo: str = ""):
    win32.send_combo(combo)
    return {"status": "sent", "combo": combo}


@command("mousemove", "Move the cursor by (dx, dy).", hide=True)
def mousemove(dx: int = 0, dy: int = 0):
    x, y = win32.MOUSE.move_by(dx, dy)
    return {"status": "moved", "x": x, "y": y}


@command("mouseclick", "Click (left/right/middle).", hide=True)
def mouseclick(button: str = "left"):
    win32.MOUSE.click(button)
    return {"status": "clicked", "button": button}


@command("mousedrag", "Press (action=down) or release (action=up) a button.", hide=True)
def mousedrag(action: str = "down", button: str = "left"):
    win32.MOUSE.button(button, action != "up")
    return {"status": "drag_" + action, "button": button}
