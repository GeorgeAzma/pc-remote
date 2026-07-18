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
import asyncio
import ctypes
import inspect
import json
import os
import queue
import subprocess
import threading
import time
from ctypes import wintypes
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# Hide the console window for child processes (so PowerShell doesn't flash).
CREATE_NO_WINDOW = 0x08000000
_SUBPROC_KWARGS = {"creationflags": CREATE_NO_WINDOW}

# --- Coalescing live-setter worker ---------------------------------------
# Live sliders (volume, brightness) fire a request on every drag tick. If the
# hardware apply is slower than the drag, a plain FIFO queue makes the slider
# lag behind the user as stale intermediate values are applied one by one.
#
# Each setter runs on a dedicated worker thread that owns its hardware handle
# (e.g. the STA-bound winrt controller). A set request registers its value as
# the "latest pending" and parks waiting for a reply. The worker applies only
# the *newest* pending value, skipping any superseded while it was busy, and
# releases those waiters immediately. So the latest drag always jumps ahead of
# the queue and is applied as soon as the current apply finishes. Gets run on
# the worker too (so they share the same thread-affine handle).
class _CoalescingSetter:
    def __init__(self, name: str):
        self._name = name
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._pending: object | None = None   # newest un-applied set value
        self._waiters: list = []               # [(value, Event, result_box)]
        self._gets: list = []                  # [(Event, result_box)]
        self._thread: threading.Thread | None = None
        self._ready = False                     # setup() completed
        self._setup_err: str | None = None

    # --- to override / configure ---
    def setup(self):
        """Called once on the worker thread before serving. Own hardware here."""
        pass

    def apply(self, value):
        """Apply one value to the hardware. Runs on the worker thread."""
        raise NotImplementedError

    def read(self):
        """Read current value from the hardware. Runs on the worker thread."""
        return None

    # --- worker loop ---
    def _worker(self):
        try:
            self.setup()
        except Exception as exc:
            with self._lock:
                self._setup_err = repr(exc)
                self._ready = True
                self._cond.notify_all()
            return
        with self._lock:
            self._ready = True
            self._cond.notify_all()
        while True:
            with self._lock:
                # Wait for a set or a get.
                while self._pending is None and not self._gets:
                    self._cond.wait()
                value = self._pending
                self._pending = None
                waiters = self._waiters
                self._waiters = []
                gets = self._gets
                self._gets = []
            # Service gets first (cheap, and keeps reads fresh).
            for ev, box in gets:
                try:
                    box[0] = ("ok", self.read())
                except Exception as exc:
                    box[0] = ("err", repr(exc))
                ev.set()
            # Apply the newest pending set value once.
            if value is not None:
                err = None
                try:
                    self.apply(value)
                except Exception as exc:
                    err = repr(exc)
                for w_value, ev, box in waiters:
                    box[0] = err if w_value is value else None
                    ev.set()

    def _ensure(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._worker, name=self._name,
                                        daemon=True)
        self._thread.start()
        # Wait for setup so callers see init errors instead of a silent hang.
        with self._lock:
            while not self._ready:
                self._cond.wait()
        if self._setup_err is not None:
            raise RuntimeError(f"{self._name} setup failed: {self._setup_err}")

    def set(self, value) -> None:
        """Submit a value; block until it is applied or superseded."""
        self._ensure()
        ev = threading.Event()
        box: list = [None]
        with self._lock:
            self._pending = value
            self._waiters.append((value, ev, box))
            self._cond.notify_all()
        ev.wait(timeout=10)
        if box[0] is not None:
            raise RuntimeError(box[0])

    def get(self):
        """Read current value; runs on the worker thread."""
        self._ensure()
        ev = threading.Event()
        box: list = [None]
        with self._lock:
            self._gets.append((ev, box))
            self._cond.notify_all()
        ev.wait(timeout=10)
        status, detail = box[0]
        if status == "err":
            raise RuntimeError(detail)
        return detail


# --- System master volume via Windows.Media.Devices (winrt) ---
# Core Audio COM (MMDeviceEnumerator) is not registered on this machine, and
# the legacy winmm waveOut mixer does not move the real WASAPI system volume.
# The UWP AudioDeviceController (obtained via a MediaCapture instance) does.
# The controller is STA-bound, so it lives on the worker thread.
class _VolumeSetter(_CoalescingSetter):
    def setup(self):
        import winrt.runtime
        winrt.runtime.init_apartment(winrt.runtime.ApartmentType.SINGLE_THREADED)
        import winrt.windows.media.capture as capture
        import winrt.windows.media.devices as devices
        self._loop = asyncio.new_event_loop()
        # Bind MediaCapture to the default *render* endpoint so it works even
        # when no capture device is present (MediaCapture otherwise demands one).
        render_id = devices.MediaDevice.get_default_audio_render_id(
            devices.AudioDeviceRole.DEFAULT)
        settings = capture.MediaCaptureInitializationSettings()
        settings.audio_device_id = render_id
        settings.streaming_capture_mode = capture.StreamingCaptureMode.AUDIO
        mc = capture.MediaCapture()
        self._loop.run_until_complete(mc.initialize_with_settings_async(settings))
        self._mc = mc  # keep MediaCapture alive or the controller gets closed
        self._ctrl = mc.audio_device_controller

    def apply(self, level):
        self._ctrl.volume_percent = max(0.0, min(100.0, float(level)))

    def read(self):
        return int(round(self._ctrl.volume_percent))


_VOL_SETTER = _VolumeSetter("volume")


def _set_master_volume(level: int) -> None:
    _VOL_SETTER.set(level)


def _get_master_volume() -> int:
    return _VOL_SETTER.get()


# --- Monitor brightness via DDC/CI (dxva2.dll) ---
# WMI WmiMonitorBrightnessMethods is not supported on this display; DDC/CI
# through dxva2.dll SetMonitorBrightness works (verified on Alienware AW2725DF).
class _PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [("hPhysicalMonitor", ctypes.c_void_p),
                ("szPhysicalMonitorDescription", ctypes.c_wchar * 128)]


_user32 = ctypes.windll.user32
_dxva2 = ctypes.windll.dxva2
_dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [wintypes.HMONITOR,
                                                           ctypes.POINTER(wintypes.DWORD)]
_dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL
_dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = [wintypes.HMONITOR, wintypes.DWORD,
                                                   ctypes.POINTER(_PHYSICAL_MONITOR)]
_dxva2.GetPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL
_dxva2.DestroyPhysicalMonitors.argtypes = [wintypes.DWORD, ctypes.POINTER(_PHYSICAL_MONITOR)]
_dxva2.DestroyPhysicalMonitors.restype = wintypes.BOOL
_dxva2.SetMonitorBrightness.argtypes = [ctypes.c_void_p, wintypes.SHORT]
_dxva2.SetMonitorBrightness.restype = wintypes.BOOL
_dxva2.GetMonitorBrightness.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.SHORT),
                                        ctypes.POINTER(wintypes.SHORT), ctypes.POINTER(wintypes.SHORT)]
_dxva2.GetMonitorBrightness.restype = wintypes.BOOL


def _primary_physical_monitor():
    """Return (handle, pm_array) for the primary monitor, or (None, None)."""
    hwnd = _user32.GetDesktopWindow()
    hmon = _user32.MonitorFromWindow(hwnd, 1)  # MONITOR_DEFAULTTOPRIMARY
    if not hmon:
        return None, None
    n = wintypes.DWORD(0)
    if not _dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmon, ctypes.byref(n)) or n.value == 0:
        return None, None
    pm = (_PHYSICAL_MONITOR * n.value)()
    if not _dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, n.value, pm):
        return None, None
    return pm[0].hPhysicalMonitor, pm


class _BrightnessSetter(_CoalescingSetter):
    """DDC/CI brightness on the primary monitor, coalesced for live sliders."""
    def apply(self, level):
        handle, pm = _primary_physical_monitor()
        if handle is None:
            raise RuntimeError("no DDC/CI physical monitor")
        try:
            target = wintypes.SHORT(max(0, min(100, int(level))))
            if not _dxva2.SetMonitorBrightness(handle, target):
                raise RuntimeError("SetMonitorBrightness failed")
        finally:
            _dxva2.DestroyPhysicalMonitors(1, pm)

    def read(self):
        handle, pm = _primary_physical_monitor()
        if handle is None:
            return None
        try:
            cur = wintypes.SHORT(0)
            minb = wintypes.SHORT(0)
            maxb = wintypes.SHORT(0)
            if _dxva2.GetMonitorBrightness(handle, ctypes.byref(minb),
                                           ctypes.byref(cur), ctypes.byref(maxb)):
                return int(cur.value)
            return None
        finally:
            _dxva2.DestroyPhysicalMonitors(1, pm)


_BRIGHTNESS_SETTER = _BrightnessSetter("brightness")


def _set_brightness_ddc(level: int) -> bool:
    try:
        _BRIGHTNESS_SETTER.set(level)
        return True
    except Exception:
        return False


def _get_brightness_ddc() -> int | None:
    return _BRIGHTNESS_SETTER.get()


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
             tab: str = "", live: bool = False) -> callable:
    """Register a function as a command/endpoint. confirm=True asks for a
    tap-to-confirm; primary=True pins the card to the top of the UI;
    undo=True shows a Cancel button in its result; hide=True keeps it callable
    but off the UI; ping=True measures real client<->server latency in the UI;
    range=["param"] renders that int param as a 0-100 slider in the UI;
    tab="media"|"tools"|"power" groups non-primary commands under a
    collapsible section of that name; live=True fires the command immediately
    whenever a parameter changes (slider drag, toggle, text input), instead of
    waiting for a tap on the card."""
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
            "live": live,
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


@command("lock", "Lock the workstation.", tab="power")
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

@command("monitor", "Turn the monitor on or off.", tab="power", live=True)
def monitor(on: bool = False):
    HWND_BROADCAST = 0xFFFF
    WM_SYSCOMMAND = 0x0112
    SC_MONITORPOWER = 0xF170
    if on:
        # SC_MONITORPOWER(-1) alone is unreliable once the display is fully
        # off; ES_DISPLAY_REQUIRED forces the display back on, and a tiny
        # synthetic mouse move guarantees the wake is registered.
        ctypes.windll.user32.SendMessageW(HWND_BROADCAST, WM_SYSCOMMAND,
                                          SC_MONITORPOWER, -1)
        ctypes.windll.kernel32.SetThreadExecutionState(0x00000002)  # ES_DISPLAY_REQUIRED
        ctypes.windll.user32.mouse_event(0x0001, 0, 1, 0, 0)  # MOUSEEVENTF_MOVE
        ctypes.windll.user32.mouse_event(0x0001, 0, 0, 0, 0)
    else:
        ctypes.windll.user32.SendMessageW(HWND_BROADCAST, WM_SYSCOMMAND,
                                          SC_MONITORPOWER, 2)
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


@command("brightness", "Set monitor brightness (0-100).", range=["level"], tab="media", live=True)
def brightness(level: int = 50):
    level = max(0, min(100, int(level)))
    if _set_brightness_ddc(level):
        return {"status": "set", "level": level}
    return {"status": "error",
            "detail": "DDC/CI brightness not supported on this display",
            "hint": "enable DDC/CI in the monitor's OSD, or the display may not expose it"}


@command("volume", "Set system volume (0-100).", range=["level"], tab="media", live=True)
def volume(level: int = 50):
    level = max(0, min(100, int(level)))
    _set_master_volume(level)
    return {"status": "set", "volume": _get_master_volume()}


@command("play", "Toggle media play/pause.", tab="media")
def play():
    VK_MEDIA_PLAY_PAUSE = 0xB3
    ctypes.windll.user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, 0, 0)
    ctypes.windll.user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, 2, 0)
    return {"status": "toggled"}


@command("bluetooth", "Enable or disable the Bluetooth radio (needs admin).", tab="tools", live=True)
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


@command("wifi", "Enable or disable the Wi-Fi interface (needs admin).", tab="tools", live=True)
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
                f'.classList.toggle(\'open\'); this.querySelector(\'.chev\')'
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
        .details{{display:none;flex-direction:row;flex-wrap:wrap;gap:.5rem .8rem;
        padding:0 .9rem .8rem}}
        .details.open{{display:flex}}
        .field{{display:inline-flex;align-items:center;gap:.4rem;font-size:.8rem;
        color:#9aa4b2}}
        .field input:not([type=checkbox]){{background:#0f1115;border:1px solid #2a2f3a;
        color:#e6e6e6;border-radius:6px;padding:.35rem .5rem;font-size:.85rem;width:5.5rem}}
        .text-field{{flex:1 1 100%;flex-wrap:wrap}}
        .text-field input,.text-field textarea{{flex:1 1 100%;width:100%;
        min-height:2.4rem;font-family:inherit;line-height:1.4}}
        .text-field textarea{{background:#0f1115;border:1px solid #2a2f3a;
        color:#e6e6e6;border-radius:6px;padding:.35rem .5rem;font-size:.85rem;
        overflow:auto;white-space:pre-wrap;resize:vertical;
        max-height:60vh}}
        .bool-field{{gap:.5rem}}
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
        .slider{{display:flex;align-items:center;gap:.5rem;margin:.15rem 0;flex:1 1 100%}}
        .slider input[type=range]{{flex:1;width:100%;height:6px;-webkit-appearance:none;
        appearance:none;background:#2a2f3a;border-radius:3px;outline:none;cursor:pointer}}
        .slider input[type=range]::-webkit-slider-thumb{{-webkit-appearance:none;
        appearance:none;width:18px;height:18px;border-radius:50%;
        background:#4da3ff;cursor:pointer;border:2px solid #0f1115}}
        .slider input[type=range]::-moz-range-thumb{{width:18px;height:18px;
        border-radius:50%;background:#4da3ff;cursor:pointer;border:2px solid #0f1115}}
        .slider .val{{min-width:2.5rem;text-align:right;color:#e6e6e6;font-size:.85rem;
        font-variant-numeric:tabular-nums;flex:none}}
        .bool-field{{display:flex;align-items:center;justify-content:space-between;
        gap:.6rem;font-size:.85rem;color:#9aa4b2}}
        .switch{{position:relative;display:inline-block;width:44px;height:24px;flex:none}}
        .switch input{{opacity:0;width:0;height:0;margin:0}}
        .switch .track{{position:absolute;cursor:pointer;inset:0;background:#2a2f3a;
        border-radius:24px;transition:background .2s}}
        .switch .track:before{{content:"";position:absolute;height:18px;width:18px;
        left:3px;top:3px;background:#9aa4b2;border-radius:50%;
        transition:transform .2s,background .2s}}
        .switch input:checked + .track{{background:#4da3ff}}
        .switch input:checked + .track:before{{transform:translateX(20px);background:#fff}}
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
        // Read the current input values of a card into a body object.
        function readInputs(card) {{
          const inputs = card.querySelectorAll('.details input, .details textarea');
          const body = {{}};
          inputs.forEach(i => {{
            if (i.type === 'checkbox') {{ body[i.name] = i.checked; }}
            else if (i.value !== '') {{ body[i.name] = i.value; }}
          }});
          return body;
        }}
        // Auto-grow a textarea to fit its content (up to its CSS max-height,
        // after which it scrolls). Called on input and after programmatic
        // changes so multi-line paste text expands instead of staying 1 row.
        function autoGrow(el) {{
          el.style.height = 'auto';
          el.style.height = el.scrollHeight + 'px';
        }}
        // Render a response into the card's output area.
        function renderResult(card, data, ok) {{
          const out = card.querySelector('.out');
          if (data && data.result && data.result.image) {{
            out.textContent = '';
            const img = document.createElement('img');
            img.src = 'data:image/png;base64,' + data.result.image;
            out.appendChild(img);
          }} else if (data && data.result && 'text' in data.result) {{
            out.textContent = data.result.text || '(empty clipboard)';
          }} else {{
            out.textContent = JSON.stringify(data, null, 2);
          }}
          out.className = 'out show' + (ok ? '' : ' err');
          card.querySelector('.details').classList.add('open');
          const cw = card.querySelector('.chev-wrap'); if (cw) cw.classList.add('show');
          const ch = card.querySelector('.chev'); if (ch) ch.classList.add('open');
          const u = card.querySelector('.undo');
          if (u && data && data.result && data.result.seconds > 0) u.classList.add('show');
        }}
        // run() keeps at most ONE request in flight per card. If a new change
        // arrives while one is in flight, it is stashed as "pending" and sent
        // the instant the in-flight one returns. This avoids the browser's
        // ~6-connection-per-host limit queueing stale slider values, while
        // never delaying the latest value and never dropping the final one.
        async function run(card, cmd) {{
          if (card._inFlight) {{ card._pending = true; return; }}
          const isPing = card.querySelector('.ping-flag') !== null;
          while (true) {{
            const body = readInputs(card);
            card._inFlight = true;
            card._pending = false;
            let data = null, ok = true;
            try {{
              if (isPing) {{
                const ms = await measureLatency(cmd);
                data = {{ result: {{ ms: ms }} }}; ok = true;
                card.querySelector('.out').textContent = fmtMs(ms) + 'ms';
              }} else {{
                const res = await fetch('/' + cmd + '?token=' + encodeURIComponent(TOKEN),
                  {{method:'POST', headers:{{'Content-Type':'application/json'}},
                   body: JSON.stringify(body)}});
                data = await res.json();
                ok = res.ok;
                renderResult(card, data, ok);
              }}
            }} catch (e) {{
              ok = false;
              card.querySelector('.out').textContent = String(e);
              card.querySelector('.out').className = 'out show err';
            }}
            card._inFlight = false;
            // If a newer change arrived while we were busy, loop and send it
            // immediately. Otherwise we're done.
            if (!card._pending) break;
          }}
        }}
        function onRow(card, cmd, needsConfirm, ev) {{
          if (needsConfirm) {{
            card.querySelector('.confirm').classList.add('show');
            return;
          }}
          run(card, cmd);
        }}
        function wireLive(card, cmd) {{
          if (card.dataset.live !== 'true') return;
          const inputs = card.querySelectorAll('.details input, .details textarea');
          inputs.forEach(i => {{
            i.addEventListener('input', () => {{
              card.querySelector('.details').classList.add('open');
              const cw = card.querySelector('.chev-wrap'); if (cw) cw.classList.add('show');
              const ch = card.querySelector('.chev'); if (ch) ch.classList.add('open');
              run(card, cmd);
            }});
          }});
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
        document.querySelectorAll('.card[data-live="true"]').forEach(card => {{
          wireLive(card, card.dataset.cmd);
        }});
        // Auto-grow textareas on input and size them once on load.
        document.querySelectorAll('textarea').forEach(t => {{
          autoGrow(t);
          t.addEventListener('input', () => autoGrow(t));
        }});
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
        def _field(p):
            nm = p["name"]
            if nm in rng:
                val = p["default"] if p["has_default"] else 50
                return (f'<div class="slider"><input name="{nm}" type="range" '
                        f'min="0" max="100" value="{val}" '
                        f'oninput="this.nextElementSibling.textContent=this.value">'
                        f'<span class="val">{val}</span></div>')
            if p["type"] == "bool":
                checked = " checked" if (p["has_default"] and p["default"] is True) else ""
                return (f'<label class="field bool-field">{nm}'
                        f'<span class="switch"><input type="checkbox" name="{nm}"{checked}>'
                        f'<span class="track"></span></span></label>')
            itype = "number" if p["type"] in ("int", "float") else "text"
            ph = p["default"] if p["has_default"] else ""
            cls = "field text-field" if p["type"] == "str" else "field"
            if p["type"] == "str":
                # Use a <textarea> so multi-line input (e.g. paste) works.
                return (f'<label class="{cls}">{nm}'
                        f'<textarea name="{nm}" placeholder="{ph}" '
                        f'rows="1"></textarea></label>')
            return (f'<label class="{cls}">{nm}'
                    f'<input name="{nm}" type="{itype}" placeholder="{ph}"></label>')
        fields = "".join(_field(p) for p in params)
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
        live = "true" if meta.get("live") else "false"
        return (f'<div class="card" data-cmd="{name}" data-live="{live}"><div class="row" '
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
    