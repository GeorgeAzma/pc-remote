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

# Make the process DPI-aware so screen capture gets the real physical
# resolution (e.g. 2560x1440) instead of the scaled logical size (2048x1152).
# Must be set before any GetSystemMetrics call and can only be set once.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

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


# --- Fast dependency-free screen capture (GDI+ via ctypes) ---
# PowerShell-based capture is ~300-500ms per frame (process spawn + .NET).
# This GDI+ path runs in-process and captures + JPEG-encodes in ~30-60ms,
# which is fast enough for a ~15fps trackpad preview without any deps.
_gdi_initialized = False
_gdi_lock = threading.Lock()


def _init_gdi():
    global _gdi_initialized
    if _gdi_initialized:
        return
    with _gdi_lock:
        if _gdi_initialized:
            return
        gdi = ctypes.windll.gdiplus
        # GdiplusStartup
        class _StartupInput(ctypes.Structure):
            _fields_ = [("GdiplusVersion", ctypes.c_uint32),
                        ("DebugEventCallback", ctypes.c_void_p),
                        ("SuppressBackgroundThread", ctypes.c_int),
                        ("SuppressExternalCodecs", ctypes.c_int)]
        si = _StartupInput()
        si.GdiplusVersion = 1
        token = ctypes.c_ulong(0)
        gdi.GdiplusStartup.argtypes = [ctypes.POINTER(ctypes.c_ulong),
                                       ctypes.c_void_p, ctypes.c_void_p]
        gdi.GdiplusStartup(ctypes.byref(token), ctypes.byref(si), None)
        _gdi_initialized = True


def _draw_cursor(mdc, scale: int = 2):
    """Draw the current mouse cursor onto a memory DC, enlarged by `scale`.

    Uses GetCursorInfo (handles hidden cursors and the I-beam etc.) and
    DrawIconEx to blit the icon sprite at the cursor's screen position.
    """
    _draw_cursor_at(mdc, None, None, scale=scale)


def _draw_cursor_at(mdc, x, y, scale: int = 2):
    """Draw the cursor at (x, y) in DC-local coordinates.

    If x/y are None, the current screen cursor position is used (the DC
    is assumed to be a full-screen capture). Otherwise the caller supplies
    the translated position — used by zoom mode where the DC only contains
    a sub-rect of the screen.
    """
    class _CURSORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint32),
                    ("flags", ctypes.c_uint32),
                    ("hCursor", ctypes.c_void_p),
                    ("ptScreenPos", ctypes.c_long * 2)]

    ci = _CURSORINFO()
    ci.cbSize = ctypes.sizeof(ci)
    user32 = ctypes.windll.user32
    user32.GetCursorInfo.argtypes = [ctypes.POINTER(_CURSORINFO)]
    user32.GetCursorInfo.restype = wintypes.BOOL
    if not user32.GetCursorInfo(ctypes.byref(ci)):
        return
    # flags & 1 == CURSOR_SHOWING; if not, the cursor is hidden.
    if not (ci.flags & 1) or not ci.hCursor:
        return
    if x is None or y is None:
        x, y = ci.ptScreenPos[0], ci.ptScreenPos[1]
    # Get the icon's nominal size so we can scale it up.
    class _ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                    ("yHotspot", wintypes.DWORD), ("hbmMask", ctypes.c_void_p),
                    ("hbmColor", ctypes.c_void_p)]
    user32.GetIconInfo.argtypes = [ctypes.c_void_p,
                                   ctypes.POINTER(_ICONINFO)]
    user32.GetIconInfo.restype = wintypes.BOOL
    ii = _ICONINFO()
    if not user32.GetIconInfo(ci.hCursor, ctypes.byref(ii)):
        return
    # Icon dimensions come from the color bitmap (or mask if mono).
    gdi32 = ctypes.windll.gdi32
    gdi32.GetBitmapBits.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.c_void_p]

    class _BITMAP(ctypes.Structure):
        _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long),
                    ("bmHeight", ctypes.c_long), ("bmWidthBytes", ctypes.c_long),
                    ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                    ("bmBits", ctypes.c_void_p)]
    bm = _BITMAP()
    src = ii.hbmColor if ii.hbmColor else ii.hbmMask
    gdi32.GetObjectW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                 ctypes.POINTER(_BITMAP)]
    gdi32.GetObjectW(src, ctypes.sizeof(bm), ctypes.byref(bm))
    iw, ih = int(bm.bmWidth), int(bm.bmHeight)
    if iw <= 0 or ih <= 0:
        iw, ih = 32, 32  # fallback
    # Hotspot offset (where the click point is within the icon).
    hx, hy = int(ii.xHotspot), int(ii.yHotspot)
    # Clean up the bitmaps GetIconInfo created (caller must free them).
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    if ii.hbmMask:
        gdi32.DeleteObject(ii.hbmMask)
    if ii.hbmColor:
        gdi32.DeleteObject(ii.hbmColor)
    # Draw the icon scaled up, offset so the hotspot stays under the cursor.
    user32.DrawIconEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                  wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    user32.DrawIconEx.restype = wintypes.BOOL
    draw_x = x - hx * scale
    draw_y = y - hy * scale
    user32.DrawIconEx(mdc, draw_x, draw_y, ci.hCursor,
                      iw * scale, ih * scale, 0, None, 0x00000003)  # DI_NORMAL


def _capture_screen_jpeg(quality: int = 55, max_w: int = 1280) -> bytes:
    """Capture the primary screen as a JPEG byte string (fast, in-process)."""
    _init_gdi()
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    gdi = ctypes.windll.gdiplus
    sw = user32.GetSystemMetrics(0)
    sh = user32.GetSystemMetrics(1)
    if sw <= 0 or sh <= 0:
        return b""
    hdc = user32.GetDC(0)
    mdc = gdi32.CreateCompatibleDC(hdc)
    hbmp = gdi32.CreateCompatibleBitmap(hdc, sw, sh)
    gdi32.SelectObject(mdc, hbmp)
    gdi32.BitBlt(mdc, 0, 0, sw, sh, hdc, 0, 0, 0x00CC0020)  # SRCCOPY
    user32.ReleaseDC(0, hdc)
    # Draw the mouse cursor onto the capture. BitBlt copies the framebuffer
    # but not the hardware cursor sprite, so the trackpad preview would show
    # no cursor at all. We draw it 2x larger so it's easy to see on a phone.
    _draw_cursor(mdc, scale=2)
    # GDI+ Bitmap from HBITMAP.
    gdi.GdipCreateBitmapFromHBITMAP.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                ctypes.POINTER(ctypes.c_void_p)]
    gdi.GdipCreateBitmapFromHBITMAP.restype = ctypes.c_int
    bmp = ctypes.c_void_p(0)
    status = gdi.GdipCreateBitmapFromHBITMAP(hbmp, None, ctypes.byref(bmp))
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(mdc)
    if status != 0 or not bmp.value:
        return b""
    # JPEG encoder CLSID: {557CF401-1A04-11D3-9A73-0000F81EF32E}
    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                    ("Data3", ctypes.c_uint16),
                    ("Data4", ctypes.c_ubyte * 8)]
    enc = _GUID(0x557CF401, 0x1A04, 0x11D3,
                (ctypes.c_ubyte * 8)(0x9A, 0x73, 0x00, 0x00, 0xF8, 0x1E, 0xF3, 0x2E))
    # EncoderParameters with one Quality parameter.
    class _EncoderParameter(ctypes.Structure):
        _fields_ = [("Guid", _GUID), ("NumberOfValues", ctypes.c_uint32),
                    ("Type", ctypes.c_uint32), ("Value", ctypes.c_void_p)]
    class _EncoderParameters(ctypes.Structure):
        _fields_ = [("Count", ctypes.c_uint32), ("Parameter", _EncoderParameter)]
    params = _EncoderParameters()
    params.Count = 1
    # Quality encoder parameter GUID: {1D5BE4B5-FA4A-4520-9B3C-181A0E770A07}
    params.Parameter.Guid = _GUID(0x1D5BE4B5, 0xFA4A, 0x4520,
                                  (ctypes.c_ubyte * 8)(0x9B, 0x3C, 0x18, 0x1A,
                                                       0x0E, 0x77, 0x0A, 0x07))
    params.Parameter.NumberOfValues = 1
    params.Parameter.Type = 1  # EncoderParameterValueTypeLong
    q = ctypes.c_uint32(max(1, min(100, int(quality))))
    params.Parameter.Value = ctypes.cast(ctypes.pointer(q), ctypes.c_void_p)
    # Save to a temp file (robust; avoids fragile IStream vtable reads).
    import tempfile
    tmp = tempfile.mktemp(suffix=".jpg")
    wpath = tmp.replace("\\", "\\\\")
    gdi.GdipSaveImageToFile.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                        ctypes.c_void_p, ctypes.c_void_p]
    gdi.GdipSaveImageToFile.restype = ctypes.c_int
    status = gdi.GdipSaveImageToFile(bmp, tmp, ctypes.byref(enc), ctypes.byref(params))
    gdi.GdipDisposeImage.argtypes = [ctypes.c_void_p]
    gdi.GdipDisposeImage(bmp)
    if status != 0:
        return b""
    try:
        with open(tmp, "rb") as f:
            data = f.read()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return data


# --- Efficient screen streaming with change detection ---
# Optimizations vs the old /stream HTTP polling:
#   1. Server-push over WebSocket — no per-frame HTTP header overhead
#   2. Change detection — samples pixels via GetDIBits and skips JPEG
#      encoding entirely when the screen hasn't changed (saves ~30ms/frame)
#   3. Downscaling — StretchBlt to max_w before encoding (4x smaller payload)
#   4. In-memory JPEG via IStream — no temp file disk I/O

class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


_ole32 = ctypes.windll.ole32
_ole32.CreateStreamOnHGlobal.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                          ctypes.POINTER(ctypes.c_void_p)]
_ole32.CreateStreamOnHGlobal.restype = ctypes.c_long  # HRESULT
_ole32.GetHGlobalFromStream.argtypes = [ctypes.c_void_p,
                                         ctypes.POINTER(ctypes.c_void_p)]
_ole32.GetHGlobalFromStream.restype = ctypes.c_long

_k32 = ctypes.windll.kernel32
_k32.GlobalLock.argtypes = [ctypes.c_void_p]
_k32.GlobalLock.restype = ctypes.c_void_p
_k32.GlobalSize.argtypes = [ctypes.c_void_p]
_k32.GlobalSize.restype = ctypes.c_size_t
_k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
_k32.GlobalUnlock.restype = wintypes.BOOL

_gdi32 = ctypes.windll.gdi32
_gdi32.GetDIBits.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                            wintypes.UINT, ctypes.c_void_p,
                            ctypes.POINTER(_BITMAPINFOHEADER), wintypes.UINT]
_gdi32.GetDIBits.restype = wintypes.INT
_gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
_gdi32.SetStretchBltMode.restype = ctypes.c_int
_gdi32.StretchBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, wintypes.HDC,
                             ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, wintypes.DWORD]
_gdi32.StretchBlt.restype = wintypes.BOOL

_user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
_user32.GetCursorPos.restype = wintypes.BOOL


def _release_com(obj):
    """Call IUnknown::Release on a COM object via its vtable."""
    try:
        if not obj or not obj.value:
            return
        vtable_addr = ctypes.c_void_p.from_address(obj.value).value
        if not vtable_addr:
            return
        # Release is vtable[2] = offset 16 on x64
        release_addr = ctypes.c_void_p.from_address(vtable_addr + 16).value
        if not release_addr:
            return
        fn = ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)(release_addr)
        fn(obj)
    except Exception:
        pass


def _encode_jpeg_memory(bmp, quality):
    """Encode a GDI+ bitmap as JPEG bytes in memory via IStream (no disk I/O).
    Returns None on failure."""
    gdi = ctypes.windll.gdiplus
    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                    ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]
    enc = _GUID(0x557CF401, 0x1A04, 0x11D3,
                (ctypes.c_ubyte * 8)(0x9A, 0x73, 0x00, 0x00, 0xF8, 0x1E, 0xF3, 0x2E))
    class _EP(ctypes.Structure):
        _fields_ = [("Guid", _GUID), ("NumberOfValues", ctypes.c_uint32),
                    ("Type", ctypes.c_uint32), ("Value", ctypes.c_void_p)]
    class _EPS(ctypes.Structure):
        _fields_ = [("Count", ctypes.c_uint32), ("Parameter", _EP)]
    params = _EPS()
    params.Count = 1
    params.Parameter.Guid = _GUID(0x1D5BE4B5, 0xFA4A, 0x4520,
        (ctypes.c_ubyte * 8)(0x9B, 0x3C, 0x18, 0x1A, 0x0E, 0x77, 0x0A, 0x07))
    params.Parameter.NumberOfValues = 1
    params.Parameter.Type = 1
    q = ctypes.c_uint32(max(1, min(100, int(quality))))
    params.Parameter.Value = ctypes.cast(ctypes.pointer(q), ctypes.c_void_p)
    stream = ctypes.c_void_p(0)
    hr = _ole32.CreateStreamOnHGlobal(None, True, ctypes.byref(stream))
    if hr != 0 or not stream.value:
        return None
    gdi.GdipSaveImageToStream.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_void_p, ctypes.c_void_p]
    gdi.GdipSaveImageToStream.restype = ctypes.c_int
    status = gdi.GdipSaveImageToStream(bmp, stream, ctypes.byref(enc),
                                       ctypes.byref(params))
    if status != 0:
        _release_com(stream)
        return None
    hg = ctypes.c_void_p(0)
    _ole32.GetHGlobalFromStream(stream, ctypes.byref(hg))
    if not hg.value:
        _release_com(stream)
        return None
    size = _k32.GlobalSize(hg)
    ptr = _k32.GlobalLock(hg)
    if not ptr:
        _release_com(stream)
        return None
    try:
        data = ctypes.string_at(ptr, size)
    finally:
        _k32.GlobalUnlock(hg)
    _release_com(stream)
    return data


class _ScreenStreamer:
    """Efficient screen capture with change detection and downscaling.

    Only encodes a JPEG when the screen content has changed (sampled pixel
    comparison via GetDIBits), and downscales to max_w before encoding to
    reduce both encode time and payload size. Designed for server-push over
    WebSocket — the server pushes frames as fast as it can capture them.
    """

    def __init__(self):
        self._prev_sample = b""
        self._prev_cursor = (-1, -1)
        self._buf = None

    def get_frame(self, quality=50, max_w=1280, zoom=1):
        """Return JPEG bytes if the screen changed, or None if unchanged.

        When zoom > 1, only a (1/zoom) region centered on the mouse cursor
        is captured and scaled up to max_w — effectively a magnifier that
        follows the cursor. This doubles per-pixel detail at the same output
        width, which keeps text legible without increasing payload size.

        In zoom mode we capture ONLY the crop region directly from the
        screen DC (not the full screen then crop), which is ~10x less work
        per frame and keeps latency low even at high zoom.
        """
        _init_gdi()
        gdi = ctypes.windll.gdiplus
        sw = _user32.GetSystemMetrics(0)
        sh = _user32.GetSystemMetrics(1)
        if sw <= 0 or sh <= 0:
            return None
        # Check cursor position (cheap — skip full compare if nothing moved)
        pt = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        cursor_pos = (pt.x, pt.y)
        # Compute the capture region. In zoom mode this is a sub-rect of
        # the screen centered on the cursor; otherwise it's the full screen.
        if zoom > 1:
            crop_w = max(1, sw // zoom)
            crop_h = max(1, sh // zoom)
            cx, cy = cursor_pos
            crop_x = max(0, min(cx - crop_w // 2, sw - crop_w))
            crop_y = max(0, min(cy - crop_h // 2, sh - crop_h))
        else:
            crop_x, crop_y, crop_w, crop_h = 0, 0, sw, sh
        # Capture ONLY the crop region directly from the screen DC.
        hdc = _user32.GetDC(0)
        mdc = _gdi32.CreateCompatibleDC(hdc)
        hbmp = _gdi32.CreateCompatibleBitmap(hdc, crop_w, crop_h)
        _gdi32.SelectObject(mdc, hbmp)
        _gdi32.BitBlt(mdc, 0, 0, crop_w, crop_h, hdc, crop_x, crop_y, 0x00CC0020)
        _user32.ReleaseDC(0, hdc)
        # Change detection: sample pixels BEFORE drawing the cursor (the
        # cursor sprite can blink/anti-alias and cause false positives).
        bi = _BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(bi)
        bi.biWidth = crop_w
        bi.biHeight = crop_h
        bi.biPlanes = 1
        bi.biBitCount = 32
        bi.biCompression = 0  # BI_RGB
        buf_size = crop_w * crop_h * 4
        if self._buf is None or len(self._buf) < buf_size:
            self._buf = (ctypes.c_ubyte * buf_size)()
        _gdi32.GetDIBits(mdc, hbmp, 0, crop_h, self._buf, ctypes.byref(bi), 0)
        sample = bytes(self._buf[::4096])
        changed = (sample != self._prev_sample or
                   cursor_pos != self._prev_cursor)
        self._prev_sample = sample
        self._prev_cursor = cursor_pos
        if not changed:
            _gdi32.DeleteObject(hbmp)
            _gdi32.DeleteDC(mdc)
            return None
        # Draw the cursor. In zoom mode the cursor's screen position must
        # be translated into crop-local coordinates.
        if zoom > 1:
            _draw_cursor_at(mdc, cursor_pos[0] - crop_x,
                            cursor_pos[1] - crop_y, scale=2)
        else:
            _draw_cursor(mdc, scale=2)
        # Downscale the crop to max_w if needed.
        if max_w > 0 and crop_w > max_w:
            out_w = max_w
            out_h = int(crop_h * max_w / crop_w)
            hdc2 = _user32.GetDC(0)
            mdc2 = _gdi32.CreateCompatibleDC(hdc2)
            hbmp2 = _gdi32.CreateCompatibleBitmap(hdc2, out_w, out_h)
            _user32.ReleaseDC(0, hdc2)
            _gdi32.SelectObject(mdc2, hbmp2)
            _gdi32.SetStretchBltMode(mdc2, 3)  # HALFTONE
            _gdi32.StretchBlt(mdc2, 0, 0, out_w, out_h, mdc,
                              0, 0, crop_w, crop_h, 0x00CC0020)
            _gdi32.DeleteObject(hbmp)
            _gdi32.DeleteDC(mdc)
            mdc, hbmp, sw, sh = mdc2, hbmp2, out_w, out_h
        else:
            sw, sh = crop_w, crop_h
        # Create GDI+ bitmap and encode JPEG in memory
        gdi.GdipCreateBitmapFromHBITMAP.argtypes = [ctypes.c_void_p,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        gdi.GdipCreateBitmapFromHBITMAP.restype = ctypes.c_int
        bmp = ctypes.c_void_p(0)
        status = gdi.GdipCreateBitmapFromHBITMAP(hbmp, None, ctypes.byref(bmp))
        _gdi32.DeleteObject(hbmp)
        _gdi32.DeleteDC(mdc)
        if status != 0 or not bmp.value:
            return None
        data = _encode_jpeg_memory(bmp, quality)
        gdi.GdipDisposeImage.argtypes = [ctypes.c_void_p]
        gdi.GdipDisposeImage(bmp)
        return data


# --- Mouse control (for the trackpad) ---
# Absolute positioning uses the normalized 0..65535 coordinate space that
# mouse_event expects; we map relative trackpad deltas to that space.
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_ABSOLUTE = 0x8000


def _mouse_move_relative(dx: int, dy: int):
    """Move the cursor by (dx, dy) pixels relative to its current position."""
    ctypes.windll.user32.mouse_event(MOUSEEVENTF_MOVE, int(dx), int(dy), 0, 0)


def _mouse_click(button: str = "left"):
    if button == "right":
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
    else:
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def _mouse_button_down(button: str = "left"):
    flag = MOUSEEVENTF_RIGHTDOWN if button == "right" else MOUSEEVENTF_LEFTDOWN
    ctypes.windll.user32.mouse_event(flag, 0, 0, 0, 0)


def _mouse_button_up(button: str = "left"):
    flag = MOUSEEVENTF_RIGHTUP if button == "right" else MOUSEEVENTF_LEFTUP
    ctypes.windll.user32.mouse_event(flag, 0, 0, 0, 0)


@command("mousemove", "Move the mouse cursor by a relative delta (dx, dy).", hide=True)
def mousemove(dx: int = 0, dy: int = 0):
    _mouse_move_relative(dx, dy)
    return {"status": "moved", "dx": dx, "dy": dy}


@command("mouseclick", "Click the mouse (left/right).", hide=True)
def mouseclick(button: str = "left"):
    _mouse_click(button)
    return {"status": "clicked", "button": button}


@command("mousedrag", "Begin or end a drag (hold/release a mouse button).", hide=True)
def mousedrag(action: str = "down", button: str = "left"):
    """action = 'down' to press and hold, 'up' to release."""
    if action == "up":
        _mouse_button_up(button)
    else:
        _mouse_button_down(button)
    return {"status": "drag_" + action, "button": button}


# --- Keyboard input via SendInput (type into the focused window) ---
# SendInput synthesizes real keystrokes, so the text goes into whatever
# window currently has focus — just like typing on a physical keyboard.
# Great for 2FA codes, URLs, search boxes, and passwords (never touches
# the clipboard, so clipboard loggers can't see it).

# Input type constants
INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUT(ctypes.Structure):
    # The INPUT union is sized by its largest member (MOUSEINPUT on x64).
    # Declaring only KEYBDINPUT makes sizeof(_INPUT) wrong (32 vs 40 bytes),
    # which causes SendInput to reject every call — so no keystrokes or
    # shortcuts (win+d, alt+tab, typing) ever reach the focused window.
    class _INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT),
                    ("mi", _MOUSEINPUT),
                    ("hi", _HARDWAREINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]


def _type_text(text: str):
    """Type a string into the focused window via SendInput (Unicode)."""
    user32 = ctypes.windll.user32
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT),
                                 ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    for ch in text:
        code = ord(ch)
        # Key down + key up for each Unicode character.
        inputs = (_INPUT * 2)()
        inputs[0].type = INPUT_KEYBOARD
        inputs[0].ki.wScan = code
        inputs[0].ki.dwFlags = KEYEVENTF_UNICODE
        inputs[1].type = INPUT_KEYBOARD
        inputs[1].ki.wScan = code
        inputs[1].ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
        user32.SendInput(2, inputs, ctypes.sizeof(_INPUT))


def _type_key(vk: int):
    """Press and release a single virtual key (e.g. VK_RETURN=0x0D)."""
    user32 = ctypes.windll.user32
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT),
                                 ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    inputs = (_INPUT * 2)()
    inputs[0].type = INPUT_KEYBOARD
    inputs[0].ki.wVk = vk
    inputs[1].type = INPUT_KEYBOARD
    inputs[1].ki.wVk = vk
    inputs[1].ki.dwFlags = KEYEVENTF_KEYUP
    user32.SendInput(2, inputs, ctypes.sizeof(_INPUT))


@command("type", "Type text into the focused window on the PC.", hide=True)
def type_text(text: str = ""):
    if not text:
        return {"status": "empty"}
    _type_text(text)
    return {"status": "typed", "length": len(text)}


# Virtual key codes for modifier keys and common special keys.
_VK_MAP = {
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "menu": 0x12,
    "shift": 0x10, "win": 0x5B, "meta": 0x5B, "super": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "space": 0x20, "spacebar": 0x20,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "pgup": 0x21, "pgdn": 0x22,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
    "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
    "f11": 0x7A, "f12": 0x7B,
    "prtsc": 0x2C, "printscreen": 0x2C, "scrolllock": 0x91, "pause": 0x13,
    "capslock": 0x14, "numlock": 0x90, "insert": 0x2D,
}


def _send_key_combo(combo: str):
    """Send a key combination like 'ctrl+x', 'alt+tab', 'win+d', or 'enter'.

    Modifiers (ctrl/alt/shift/win) are pressed first, then the main key, then
    everything is released in reverse order. A bare key name with no '+'
    (e.g. 'enter', 'f5') just presses that key.
    """
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        return
    user32 = ctypes.windll.user32
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT),
                                 ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    # Resolve each part to a VK code. Single chars -> their ASCII code.
    vks = []
    for p in parts:
        if p in _VK_MAP:
            vks.append(_VK_MAP[p])
        elif len(p) == 1:
            vks.append(ord(p.upper()))
        else:
            raise ValueError(f"unknown key: {p}")
    # Build the input sequence: press all keys in order, then release in reverse.
    n = len(vks) * 2
    inputs = (_INPUT * n)()
    for i, vk in enumerate(vks):
        inputs[i].type = INPUT_KEYBOARD
        inputs[i].ki.wVk = vk
    for i, vk in enumerate(reversed(vks)):
        inputs[len(vks) + i].type = INPUT_KEYBOARD
        inputs[len(vks) + i].ki.wVk = vk
        inputs[len(vks) + i].ki.dwFlags = KEYEVENTF_KEYUP
    user32.SendInput(n, inputs, ctypes.sizeof(_INPUT))


@command("keys", "Send a key combination (e.g. ctrl+x, alt+tab, win+d, enter).", hide=True)
def keys(combo: str = ""):
    if not combo.strip():
        return {"error": "no combo"}
    try:
        _send_key_combo(combo)
    except ValueError as exc:
        return {"error": str(exc)}
    return {"status": "sent", "combo": combo}


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





@command("sendlink", "Open a URL in the PC's default browser.", tab="tools")
def sendlink(url: str = ""):
    import webbrowser
    u = url.strip()
    if not u:
        return {"error": "no url"}
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    webbrowser.open(u)
    return {"status": "opened", "url": u}


@command("sendfile", "Upload a file from your phone to the PC's Downloads folder.", tab="tools")
def sendfile():
    # The actual upload is handled by the /upload endpoint (multipart POST).
    # This command exists only to render a card with a file picker in the UI.
    return {"status": "ready", "hint": "pick a file below"}


@command("trackpad", "Turn this page into a remote trackpad with a live screen preview.", primary=True)
def trackpad():
    return {"status": "ready"}


# --- Interactive terminal via ConPTY (Windows pseudo-console) ---
# ConPTY gives a real PTY, so the shell renders its own prompt, colors,
# cursor, and tab-completion exactly as in a real terminal. We spawn
# powershell.exe (or wsl bash) attached to a ConPTY, then pump bytes
# between the PTY and a WebSocket. The browser renders the raw output
# (ANSI escapes) in a <pre> via xterm.js-free minimal styling.
import struct as _struct

# kernel32 / kernelbase ConPTY functions
class _COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

_kernel32 = ctypes.windll.kernel32
_kernel32.CreatePseudoConsole.argtypes = [_COORD, wintypes.HANDLE,
                                          wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(ctypes.c_void_p)]
_kernel32.CreatePseudoConsole.restype = ctypes.c_long  # HRESULT
_kernel32.ClosePseudoConsole.argtypes = [ctypes.c_void_p]
_kernel32.ResizePseudoConsole.argtypes = [ctypes.c_void_p, _COORD]
_kernel32.ResizePseudoConsole.restype = ctypes.c_long  # HRESULT
_kernel32.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, wintypes.DWORD,
    wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]
_kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
_kernel32.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wintypes.DWORD,
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_size_t)]
_kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
_kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
_kernel32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
    ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
    ctypes.c_void_p, ctypes.c_void_p]
_kernel32.CreateProcessW.restype = wintypes.BOOL

PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400


class _STARTUPINFO(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class _STARTUPINFOEX(ctypes.Structure):
    _fields_ = [("StartupInfo", _STARTUPINFO), ("lpAttributeList", ctypes.c_void_p)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wintypes.BOOL)]


class _ConPtySession:
    """A single ConPTY-backed shell session. Lives for the duration of a
    WebSocket connection. Bytes from the shell are read on a worker thread
    and pushed to a queue; the WS handler forwards them to the browser."""

    def __init__(self, cols: int = 100, rows: int = 30, shell: str = ""):
        self.cols = max(20, min(300, cols))
        self.rows = max(5, min(100, rows))
        self.shell = shell or "ps"
        self.hPC = None
        self.hProcess = None
        self.hThread = None
        self.dwProcessId = 0
        self._pipe_in = None   # we write to this -> shell reads
        self._pipe_out = None  # shell writes to this -> we read
        self._reader = None
        self._out_q: queue.Queue = queue.Queue()
        self._closed = False

    def start(self):
        # Create a pipe pair for PTY input (we write, shell reads).
        sa = _SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(sa)
        sa.bInheritHandle = True
        pipe_in_read = wintypes.HANDLE()
        pipe_in_write = wintypes.HANDLE()
        pipe_out_read = wintypes.HANDLE()
        pipe_out_write = wintypes.HANDLE()
        if not _kernel32.CreatePipe(ctypes.byref(pipe_in_read), ctypes.byref(pipe_in_write),
                                    ctypes.byref(sa), 0):
            raise OSError("CreatePipe(in) failed")
        if not _kernel32.CreatePipe(ctypes.byref(pipe_out_read), ctypes.byref(pipe_out_write),
                                    ctypes.byref(sa), 0):
            raise OSError("CreatePipe(out) failed")
        # The shell-side ends must be inheritable; our ends must NOT be.
        _kernel32.SetHandleInformation(pipe_in_write, 2, 2)   # HANDLE_FLAG_INHERIT
        _kernel32.SetHandleInformation(pipe_out_read, 2, 2)
        _kernel32.SetHandleInformation(pipe_in_read, 2, 0)
        _kernel32.SetHandleInformation(pipe_out_write, 2, 0)
        # Create the pseudo console.
        size = _COORD(self.cols, self.rows)
        phPC = ctypes.c_void_p(0)
        hr = _kernel32.CreatePseudoConsole(size, pipe_in_read, pipe_out_write, 0,
                                           ctypes.byref(phPC))
        if hr != 0:
            raise OSError(f"CreatePseudoConsole failed: 0x{hr & 0xFFFFFFFF:08X}")
        self.hPC = phPC
        # Close the shell-side pipe ends (the ConPTY owns them now).
        _kernel32.CloseHandle(pipe_in_read)
        _kernel32.CloseHandle(pipe_out_write)
        self._pipe_in = pipe_in_write   # we write input here
        self._pipe_out = pipe_out_read  # we read output here
        # Build the proc thread attribute list with the pseudoconsole.
        attr_size = ctypes.c_size_t(0)
        _kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(attr_size))
        buf = (ctypes.c_byte * attr_size.value)()
        attr_list = ctypes.cast(buf, ctypes.c_void_p)
        if not _kernel32.InitializeProcThreadAttributeList(attr_list, 1, 0,
                                                           ctypes.byref(attr_size)):
            raise OSError("InitializeProcThreadAttributeList failed")
        if not _kernel32.UpdateProcThreadAttribute(attr_list, 0,
                ctypes.c_void_p(PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE), self.hPC,
                ctypes.sizeof(ctypes.c_void_p), None, None):
            raise OSError("UpdateProcThreadAttribute failed")
        # STARTUPINFOEX.
        si = _STARTUPINFOEX()
        si.StartupInfo.cb = ctypes.sizeof(si)
        si.lpAttributeList = attr_list
        pi = _PROCESS_INFORMATION()
        # Choose the shell command line.
        if self.shell == "wsl":
            cmdline = r'wsl.exe -- bash -l'
        else:
            cmdline = r'powershell.exe -NoLogo -NoProfile'
        cmdline_buf = ctypes.create_unicode_buffer(cmdline)
        flags = EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT
        ok = _kernel32.CreateProcessW(None, cmdline_buf, None, None, False, flags,
                                      None, None, ctypes.byref(si), ctypes.byref(pi))
        if not ok:
            err = ctypes.get_last_error()
            raise OSError(f"CreateProcessW failed: {err}")
        self.hProcess = pi.hProcess
        self.hThread = pi.hThread
        self.dwProcessId = pi.dwProcessId
        self._attr_list = attr_list
        self._attr_buf = buf
        # Start the output reader thread.
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        """Read output from the ConPTY and push to the queue."""
        buf = (ctypes.c_char * 4096)()
        while not self._closed:
            n = wintypes.DWORD(0)
            ok = _kernel32.ReadFile(self._pipe_out, buf, 4096, ctypes.byref(n), None)
            if not ok or n.value == 0:
                break
            self._out_q.put(buf.raw[:n.value])

    def write(self, data: bytes):
        """Send input to the shell."""
        if self._closed or self._pipe_in is None:
            return
        written = wintypes.DWORD(0)
        _kernel32.WriteFile(self._pipe_in, data, len(data), ctypes.byref(written), None)

    def resize(self, cols: int, rows: int):
        if self._closed or self.hPC is None:
            return
        size = _COORD(max(20, min(300, cols)), max(5, min(100, rows)))
        _kernel32.ResizePseudoConsole(self.hPC, size)

    def read(self, timeout: float = 0.05) -> bytes | None:
        try:
            return self._out_q.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self.hPC:
                _kernel32.ClosePseudoConsole(self.hPC)
        except Exception:
            pass
        try:
            if self._pipe_in:
                _kernel32.CloseHandle(self._pipe_in)
        except Exception:
            pass
        try:
            if self._pipe_out:
                _kernel32.CloseHandle(self._pipe_out)
        except Exception:
            pass
        try:
            if self.hProcess:
                _kernel32.WaitForSingleObject(self.hProcess, 1000)
                _kernel32.CloseHandle(self.hProcess)
        except Exception:
            pass
        try:
            if self.hThread:
                _kernel32.CloseHandle(self.hThread)
        except Exception:
            pass
        try:
            if self._attr_list:
                _kernel32.DeleteProcThreadAttributeList(self._attr_list)
        except Exception:
            pass


@command("terminal", "Open an interactive terminal (PowerShell or WSL bash) with a real prompt, colors, and history.", primary=True)
def terminal(shell: str = "ps"):
    return {"status": "ready", "shell": shell}


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
        if route == "/stream":
            return self._serve_stream(query)
        if route == "/vstream":
            return self._handle_vstream_ws(query)
        if route == "/ws":
            return self._handle_ws(query)
        if route == "/term":
            return self._handle_term_ws(query)
        return self._run(route.lstrip("/"), query)

    def do_POST(self):
        _record_request()
        route, query = self._parse()
        if route == "/upload":
            return self._handle_upload(query)
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        return self._run(route.lstrip("/"), query, body)

    def _handle_ws(self, query: dict):
        """Minimal WebSocket endpoint for low-latency mouse input.

        The trackpad sends tiny JSON messages like {"m":"move","dx":3,"dy":-1}
        over a single persistent connection, avoiding per-move HTTP overhead.
        We apply them directly via the mouse_* helpers and never reply (the
        client doesn't wait for a response), so latency is just network RTT.
        """
        if not self._authorized(query):
            return
        import hashlib
        import base64
        import struct
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key:
            self.send_response(400)
            self.end_headers()
            return
        # RFC 6455 handshake: accept = base64(sha1(key + magic))
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode())
            .digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        # Now we're in raw WebSocket frame mode on self.rfile/self.wfile.
        rfile = self.rfile
        wfile = self.wfile
        try:
            while True:
                # Read a frame header (2 bytes min).
                hdr = rfile.read(2)
                if len(hdr) < 2:
                    break
                b0, b1 = hdr[0], hdr[1]
                fin = b0 & 0x80
                opcode = b0 & 0x0F
                masked = b1 & 0x80
                plen = b1 & 0x7F
                if plen == 126:
                    plen = struct.unpack("!H", rfile.read(2))[0]
                elif plen == 127:
                    plen = struct.unpack("!Q", rfile.read(8))[0]
                mask = rfile.read(4) if masked else b""
                payload = rfile.read(plen)
                if masked:
                    payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
                if opcode == 0x8:  # close
                    break
                if opcode != 0x1:  # only handle text frames
                    continue
                try:
                    msg = json.loads(payload.decode("utf-8"))
                except Exception:
                    continue
                m = msg.get("m")
                if m == "move":
                    _mouse_move_relative(int(msg.get("dx", 0)), int(msg.get("dy", 0)))
                elif m == "click":
                    _mouse_click(msg.get("button", "left"))
                elif m == "down":
                    _mouse_button_down(msg.get("button", "left"))
                elif m == "up":
                    _mouse_button_up(msg.get("button", "left"))
        except (OSError, ValueError, _struct.error):
            pass  # client disconnected

    @staticmethod
    def _ws_read_frame(rfile):
        """Read one WebSocket frame from rfile. Returns (opcode, payload) or
        (None, None) on disconnect."""
        import struct
        hdr = rfile.read(2)
        if len(hdr) < 2:
            return None, None
        b0, b1 = hdr[0], hdr[1]
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        plen = b1 & 0x7F
        if plen == 126:
            plen = struct.unpack("!H", rfile.read(2))[0]
        elif plen == 127:
            plen = struct.unpack("!Q", rfile.read(8))[0]
        mask = rfile.read(4) if masked else b""
        payload = rfile.read(plen)
        if masked:
            payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
        return opcode, payload

    @staticmethod
    def _ws_write_frame(wfile, payload: bytes, opcode: int = 0x1):
        """Write a WebSocket text/binary frame to wfile (server->client,
        unmasked)."""
        import struct
        b0 = 0x80 | opcode  # FIN + opcode
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", b0, n)
        elif n < 65536:
            header = struct.pack("!BBH", b0, 126, n)
        else:
            header = struct.pack("!BBQ", b0, 127, n)
        wfile.write(header + payload)
        wfile.flush()

    def _handle_term_ws(self, query: dict):
        """WebSocket endpoint for an interactive ConPTY terminal session."""
        if not self._authorized(query):
            return
        import hashlib
        import base64
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key:
            self.send_response(400)
            self.end_headers()
            return
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode())
            .digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        shell = query.get("shell", ["ps"])[0]
        cols = int(query.get("cols", ["100"])[0])
        rows = int(query.get("rows", ["30"])[0])
        try:
            session = _ConPtySession(cols=cols, rows=rows, shell=shell)
            session.start()
        except Exception as exc:
            self._ws_write_frame(self.wfile, json.dumps(
                {"error": str(exc)}).encode("utf-8"))
            return
        rfile = self.rfile
        wfile = self.wfile
        # Run the output pump on a dedicated thread so the shell's output
        # (prompt, command results) is forwarded to the browser immediately,
        # without waiting for the client to send a message first.
        pump_stop = threading.Event()
        def _pump():
            while not pump_stop.is_set():
                chunk = session.read(timeout=0.1)
                if chunk is not None:
                    try:
                        self._ws_write_frame(wfile, chunk, opcode=0x2)
                    except (OSError, ValueError):
                        break
        pump = threading.Thread(target=_pump, daemon=True)
        pump.start()
        try:
            while True:
                opcode, payload = self._ws_read_frame(rfile)
                if opcode is None or opcode == 0x8:  # close / disconnect
                    break
                if opcode == 0x1:  # text frame = JSON control message
                    try:
                        msg = json.loads(payload.decode("utf-8"))
                    except Exception:
                        continue
                    m = msg.get("m")
                    if m == "input":
                        session.write(msg.get("data", "").encode("utf-8"))
                    elif m == "resize":
                        session.resize(int(msg.get("cols", 100)),
                                       int(msg.get("rows", 30)))
                elif opcode == 0x2:  # binary frame = raw input bytes
                    session.write(payload)
        except (OSError, ValueError, _struct.error):
            pass
        finally:
            pump_stop.set()
            session.close()

    def _handle_vstream_ws(self, query: dict):
        """WebSocket endpoint for efficient server-push screen streaming.

        The server runs a capture loop that pushes JPEG frames as binary WS
        messages whenever the screen changes. The client never requests — it
        just receives. This eliminates per-frame HTTP overhead and allows the
        server to skip encoding entirely when the screen is static.
        """
        if not self._authorized(query):
            return
        import hashlib
        import base64
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key:
            self.send_response(400)
            self.end_headers()
            return
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode())
            .digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        quality = [int(query.get("q", [50])[0])]
        max_w = [int(query.get("w", [1024])[0])]
        zoom = [int(query.get("z", [1])[0])]
        paused = [False]
        streamer = _ScreenStreamer()
        stop = threading.Event()

        def capture_loop():
            # Poll at max speed — no artificial 30fps cap. The loop runs
            # as fast as it can capture+encode+send, which on a modern PC
            # is well above 60fps and lets the phone render at its own
            # refresh rate. Only back off gently when the screen has been
            # completely static for a while, to avoid burning CPU for
            # nothing. Zoom mode never idles (cursor moves constantly).
            idle_interval = 0.2    # gentle backoff when truly static
            active_interval = 0.0  # full speed — no cap
            interval = active_interval
            static_streak = 0
            while not stop.is_set():
                if paused[0]:
                    time.sleep(0.1)
                    continue
                frame = streamer.get_frame(quality[0], max_w[0], zoom[0])
                if frame is not None:
                    try:
                        self._ws_write_frame(self.wfile, frame, opcode=0x2)
                    except (OSError, ValueError):
                        stop.set()
                        break
                    interval = active_interval  # screen active — full speed
                    static_streak = 0
                else:
                    # Screen unchanged — keep polling at full speed for a
                    # short streak, then back off gently to idle_interval.
                    static_streak += 1
                    if static_streak > 30:
                        interval = idle_interval
                    # else keep polling at full speed
                if interval > 0:
                    time.sleep(interval)
                else:
                    # Yield to let other threads run without actually
                    # sleeping — keeps latency at a minimum.
                    time.sleep(0)

        cap = threading.Thread(target=capture_loop, daemon=True)
        cap.start()
        try:
            while True:
                opcode, payload = self._ws_read_frame(self.rfile)
                if opcode is None or opcode == 0x8:
                    break
                if opcode == 0x1:  # text = JSON control message
                    try:
                        msg = json.loads(payload.decode("utf-8"))
                        m = msg.get("m")
                        if m == "pause":
                            paused[0] = True
                        elif m == "resume":
                            paused[0] = False
                        elif m == "set":
                            if "q" in msg:
                                quality[0] = int(msg["q"])
                            if "w" in msg:
                                max_w[0] = int(msg["w"])
                            if "z" in msg:
                                zoom[0] = max(1, int(msg["z"]))
                    except Exception:
                        pass
        except (OSError, ValueError, _struct.error):
            pass
        finally:
            stop.set()

    def _serve_stream(self, query: dict):
        """Single-frame screen capture as JPEG. The trackpad polls this."""
        if not self._authorized(query):
            return
        q = int(query.get("q", [55])[0])
        mw = int(query.get("w", [1280])[0])
        data = _capture_screen_jpeg(quality=q, max_w=mw)
        if not data:
            self._send(500, {"error": "capture failed"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _handle_upload(self, query: dict):
        """Receive a file upload (multipart/form-data) into the Downloads folder."""
        if not self._authorized(query):
            return
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            self._send(400, {"error": "expected multipart/form-data"})
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            self._send(400, {"error": "empty body"})
            return
        # Parse the boundary from the Content-Type header.
        boundary = None
        for part in ctype.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[len("boundary="):].strip('"')
                break
        if not boundary:
            self._send(400, {"error": "no boundary"})
            return
        raw = self.rfile.read(length)
        # Split into parts and find the first file part.
        delim = b"--" + boundary.encode()
        downloads = os.path.join(os.path.expanduser("~"), "Downloads")
        os.makedirs(downloads, exist_ok=True)
        saved = []
        for chunk in raw.split(delim):
            if not chunk or chunk == b"--" or chunk == b"--\r\n":
                continue
            # Each part: \r\n<headers>\r\n\r\n<body>\r\n
            if b"\r\n\r\n" not in chunk:
                continue
            header_blob, _, body = chunk.partition(b"\r\n\r\n")
            header_text = header_blob.decode("utf-8", "replace")
            if 'filename="' not in header_text:
                continue  # skip non-file fields
            # Extract filename.
            fname = None
            for line in header_text.split("\r\n"):
                if 'filename="' in line:
                    i = line.index('filename="') + len('filename="')
                    j = line.index('"', i)
                    fname = line[i:j]
                    break
            if not fname:
                continue
            # Strip trailing \r\n added by multipart.
            if body.endswith(b"\r\n"):
                body = body[:-2]
            # Sanitize the filename.
            safe = os.path.basename(fname)
            if not safe:
                safe = "upload"
            dest = os.path.join(downloads, safe)
            # Avoid clobbering existing files.
            base, ext = os.path.splitext(dest)
            n = 1
            while os.path.exists(dest):
                dest = f"{base} ({n}){ext}"
                n += 1
            with open(dest, "wb") as f:
                f.write(body)
            saved.append(os.path.basename(dest))
        if not saved:
            self._send(400, {"error": "no file part found"})
            return
        self._send(200, {"status": "saved", "files": saved, "folder": downloads})

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
        <meta charset="utf-8">
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
        font-size:.8rem;white-space:pre-wrap;display:none;color:#a7f3d0;
        overflow:auto;max-height:60vh}}
        .out.err{{color:#fca5a5}}
        .out.show{{display:block}}
        .out .ansi-out{{color:#c8d3e0}}
        .out .ansi-err{{color:#fca5a5}}
        .out .ansi-exit{{color:#5b6473;margin-top:.3rem;font-size:.75rem;
        border-top:1px solid #2a2f3a;padding-top:.3rem}}
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
        /* Send File card */
        .file-field{{flex:1 1 100%;display:flex;flex-direction:column;gap:.4rem}}
        .file-field input[type=file]{{color:#9aa4b2;font-size:.85rem}}
        /* Trackpad card */
        .trackpad{{display:none;flex-direction:column;gap:.5rem;padding:.6rem}}
        .trackpad.open{{display:flex}}
        .trackpad .preview{{position:relative;width:100%;background:#000;
        border:1px solid #2a2f3a;border-radius:8px;overflow:hidden;
        line-height:0;flex-shrink:0}}
        .trackpad .preview img{{width:100%;height:auto;display:block}}
        .trackpad .preview .badge{{position:absolute;top:6px;left:8px;
        font-size:.7rem;color:#9aa4b2;background:rgba(0,0,0,.5);padding:1px 6px;
        border-radius:4px}}
        .trackpad .pad{{flex:1 1 auto;min-height:180px;background:#0f1115;
        border:1px solid #2a2f3a;border-radius:10px;touch-action:none;
        user-select:none;-webkit-user-select:none;position:relative}}
        .trackpad .btns{{display:flex;gap:.5rem}}
        .trackpad .btns button{{flex:1;background:#171a21;border:1px solid #2a2f3a;
        color:#e6e6e6;padding:.6rem;border-radius:8px;font-weight:600;
        cursor:pointer;font-size:.85rem}}
        .trackpad .btns button:active{{background:#222732}}
        .trackpad .ctrls{{display:flex;align-items:center;gap:.6rem;font-size:.8rem;
        color:#9aa4b2;flex-wrap:wrap}}
        .trackpad .ctrls label{{display:flex;align-items:center;gap:.3rem}}
        .trackpad .ctrls input[type=range]{{width:120px}}
        .trackpad .ctrls .fps{{margin-left:auto;font-variant-numeric:tabular-nums}}
        .trackpad .kb-row{{display:flex;gap:.4rem}}
        .trackpad .kb-row input{{flex:1;background:#0f1115;border:1px solid #2a2f3a;
        color:#e6e6e6;border-radius:6px;padding:.4rem .6rem;font-size:.85rem}}
        .trackpad .kb-row button{{background:#4da3ff;color:#0f1115;border:0;
        padding:.4rem .7rem;border-radius:6px;font-weight:600;cursor:pointer;
        font-size:.85rem;flex:none}}
        .trackpad .kb-row button:last-child{{background:#171a21;color:#e6e6e6;
        border:1px solid #2a2f3a}}
        .trackpad .kb-shortcuts{{display:flex;gap:.3rem;flex-wrap:wrap}}
        .trackpad .kb-shortcuts button{{background:#171a21;border:1px solid #2a2f3a;
        color:#9aa4b2;padding:.3rem .5rem;border-radius:5px;cursor:pointer;
        font-size:.75rem}}
        /* Terminal card */
        .term{{display:none;flex-direction:column;gap:.4rem;padding:.5rem}}
        .term.open{{display:flex}}
        .term .screen{{background:#0c0c0c;border:1px solid #2a2f3a;border-radius:6px;
        font-family:ui-monospace,"Cascadia Code",Consolas,monospace;font-size:.78rem;
        line-height:1.35;color:#cccccc;padding:.5rem;overflow:auto;
        white-space:pre;height:10rem;touch-action:pan-y;
        -webkit-overflow-scrolling:touch}}
        .term .screen .cursor{{display:inline-block;width:.55em;height:1em;
        background:#ccc;vertical-align:bottom;animation:blink 1s step-end infinite}}
        @keyframes blink{{50%{{opacity:0}}}}
        .term .bar{{display:flex;gap:.4rem;align-items:center;font-size:.8rem;
        color:#9aa4b2;flex-wrap:wrap}}
        .term .bar select{{background:#0f1115;border:1px solid #2a2f3a;color:#e6e6e6;
        border-radius:4px;padding:.2rem .4rem;font-size:.8rem}}
        .term .bar button{{background:#171a21;border:1px solid #2a2f3a;color:#e6e6e6;
        padding:.3rem .6rem;border-radius:4px;cursor:pointer;font-size:.8rem}}
        .term .bar button:active{{background:#222732}}
        .term .bar .status{{margin-left:auto;font-size:.75rem;color:#5b6473}}
        .term .input-row{{display:flex;gap:.4rem}}
        .term .input-row input{{flex:1;background:#0f1115;border:1px solid #2a2f3a;
        color:#e6e6e6;border-radius:6px;padding:.4rem .6rem;font-family:inherit;
        font-size:.8rem}}
        .term .input-row button{{background:#4da3ff;color:#0f1115;border:0;
        padding:.4rem .8rem;border-radius:6px;font-weight:600;cursor:pointer;
        font-size:.8rem}}
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
          }} else if (data && data.result && data.result.ansi) {{
            // ANSI-colored console output (ps/wsl). Render to colored HTML.
            out.innerHTML = ansiToHtml(data.result.stdout || '',
              data.result.stderr || '', data.result.exit);
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
        // Minimal ANSI SGR -> <span style="color"> converter. Handles the
        // common 8/16-color codes plus 256-color and truecolor. Unknown SGR
        // codes reset state. Returns escaped HTML.
        const ANSI_COLORS = ['#000','#c00','#0a0','#a50','#00c','#c0c','#0aa','#ccc',
          '#777','#f00','#0f0','#ff0','#00f','#f0f','#0ff','#fff'];
        function ansiToHtml(stdout, stderr, exit) {{
          const esc = s => s.replace(/[&<>]/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;'}})[c]);
          // Build a 256-color palette (16 base + 216 cube + 24 grayscale).
          if (!window._ANSI_256) {{
            const p = ANSI_COLORS.slice();
            for (let r=0;r<6;r++) for (let g=0;g<6;g++) for (let b=0;b<6;b++)
              p.push('#' + [r,g,b].map(v => v?v*40+55:0).map(v => v.toString(16).padStart(2,'0')).join(''));
            for (let v=8;v<248;v+=10) p.push('#' + [v,v,v].map(x => x.toString(16).padStart(2,'0')).join(''));
            window._ANSI_256 = p;
          }}
          const ANSI_256 = window._ANSI_256;
          const render = (text, defaultClass) => {{
            let out = '', i = 0, open = false, cur = '';
            const flush = () => {{ if (open) out += '</span>'; open = false; cur=''; }};
            const setSpan = (style) => {{
              flush();
              out += '<span style="' + style + '">';
              open = true;
            }};
            while (i < text.length) {{
              // OSC sequence: \x1b] ... \x07 (BEL) or \x1b\\ (ST). Sets window
              // title etc. — strip it entirely, we don't use it.
              if (text[i] === '\\x1b' && text[i+1] === ']') {{
                let j = i + 2;
                while (j < text.length && text[j] !== '\\x07' &&
                       !(text[j] === '\\x1b' && text[j+1] === '\\\\')) j++;
                i = (text[j] === '\\x07') ? j + 1 : j + 2;
                continue;
              }}
              // Other lone ESC sequences (e.g. \x1b= keypad mode) — skip ESC + 1 char.
              if (text[i] === '\\x1b' && text[i+1] !== '[') {{
                i += 2;
                continue;
              }}
              if (text[i] === '\\x1b' && text[i+1] === '[') {{
                // CSI sequence. Find the final byte (0x40-0x7E).
                let j = i + 2;
                while (j < text.length && (text.charCodeAt(j) < 0x40 || text.charCodeAt(j) > 0x7E)) j++;
                if (j < text.length) {{
                  const final = text[j];
                  const params = text.slice(i+2, j);
                  if (final === 'm') {{
                    // SGR - color/style. Parse semicolon-separated codes.
                    const codes = params.split(';').map(s => s === '' ? 0 : Number(s));
                    let k = 0;
                    while (k < codes.length) {{
                      const c = codes[k];
                      if (c === 0) {{ flush(); }}
                      else if (c === 1) {{ setSpan((cur ? cur + ';' : '') + 'font-weight:bold'); cur=(cur||'')+'font-weight:bold'; }}
                      else if (c === 3) {{ setSpan((cur ? cur + ';' : '') + 'font-style:italic'); cur=(cur||'')+'font-style:italic'; }}
                      else if (c === 4) {{ setSpan((cur ? cur + ';' : '') + 'text-decoration:underline'); cur=(cur||'')+'text-decoration:underline'; }}
                      else if (c === 22) {{ flush(); }}
                      else if (c >= 30 && c <= 37) {{ setSpan('color:' + ANSI_COLORS[c-30]); cur='color:' + ANSI_COLORS[c-30]; }}
                      else if (c === 38 && codes[k+1] === 5) {{ setSpan('color:' + ANSI_256[codes[k+2]||0]); k += 2; cur='color:' + ANSI_256[codes[k]||0]; }}
                      else if (c === 38 && codes[k+1] === 2) {{ setSpan('color:rgb(' + (codes[k+2]||0) + ',' + (codes[k+3]||0) + ',' + (codes[k+4]||0) + ')'); k += 4; }}
                      else if (c >= 40 && c <= 47) {{ setSpan('background:' + ANSI_COLORS[c-40]); cur='background:' + ANSI_COLORS[c-40]; }}
                      else if (c === 39 || c === 49) {{ flush(); }}
                      else if (c >= 90 && c <= 97) {{ setSpan('color:' + ANSI_COLORS[c-90+8]); cur='color:' + ANSI_COLORS[c-90+8]; }}
                      k++;
                    }}
                  }}
                  // Other CSI codes (K=erase line, J=erase screen, H=cursor
                  // pos, etc.) are silently skipped - they don't affect text
                  // styling and would corrupt the output if interpreted.
                  i = j + 1;
                  continue;
                }}
              }}
              // Plain char.
              if (!open) {{ out += '<span class="' + defaultClass + '">'; open = true; cur = defaultClass; }}
              out += esc(text[i]);
              i++;
            }}
            flush();
            return out;
          }};
          let html = '';
          if (stdout) html += render(stdout, 'ansi-out');
          if (stderr) {{
            if (stdout) html += '<hr style="border:0;border-top:1px solid #2a2f3a;margin:.3rem 0">';
            html += render(stderr, 'ansi-err');
          }}
          if (exit !== undefined && exit !== null)
            html += '<div class="ansi-exit">exit ' + exit + '</div>';
          return html;
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
                const out = card.querySelector('.out');
                out.textContent = fmtMs(ms) + 'ms';
                out.className = 'out show';
                card.querySelector('.details').classList.add('open');
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
            if (res.ok) inp.value = '';
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
        // Wire up the terminal input box.
        document.querySelectorAll('.card[data-cmd="terminal"]').forEach(c => wireTermInput(c));

        // --- Send File: upload to /upload (multipart) ---
        async function uploadFile(card) {{
          const inp = card.querySelector('input[type=file]');
          const out = card.querySelector('.out');
          if (!inp.files.length) {{
            out.textContent = 'pick a file first'; out.className = 'out show err';
            return;
          }}
          const fd = new FormData();
          for (const f of inp.files) fd.append('file', f, f.name);
          out.textContent = 'uploading…'; out.className = 'out show';
          try {{
            const res = await fetch('/upload?token=' + encodeURIComponent(TOKEN),
              {{method:'POST', body: fd}});
            const data = await res.json();
            out.textContent = JSON.stringify(data, null, 2);
            out.className = 'out show' + (res.ok ? '' : ' err');
            if (res.ok) inp.value = '';
          }} catch (e) {{
            out.textContent = String(e); out.className = 'out show err';
          }}
        }}

        // --- Trackpad: live screen stream + touch gestures ---
        let tpStreamOn = false, tpImg = null, tpLast = null, tpFpsT = 0, tpFpsN = 0;
        let tpDragPending = false, tpDragging = false, tpDownAt = 0, tpMoved = false;
        function toggleTrackpad(card) {{
          const tp = card.querySelector('.trackpad');
          const open = tp.classList.toggle('open');
          if (open) {{ wsConnect(); startStream(card); }} else stopStream();
        }}
        function startStream(card) {{
          tpStreamOn = true; tpImg = card.querySelector('.preview img');
          const badge = card.querySelector('.badge');
          const fpsEl = card.querySelector('#tp-fps');
          tpFpsT = performance.now(); tpFpsN = 0;
          let lastUrl = null;
          const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
          const sw = new WebSocket(proto + '//' + location.host + '/vstream?token='
            + encodeURIComponent(TOKEN) + '&q=75&w=1280');
          sw.binaryType = 'arraybuffer';
          card._streamWs = sw;
          sw.onopen = () => {{
            badge.textContent = 'live';
            badge.style.background = 'rgba(34,197,94,.6)';
          }};
          sw.onclose = () => {{
            badge.textContent = 'disconnected';
            badge.style.background = 'rgba(91,100,115,.6)';
            if (tpStreamOn && card.querySelector('.trackpad.open'))
              setTimeout(() => startStream(card), 1000);
          }};
          sw.onerror = () => {{
            badge.textContent = 'error';
            badge.style.background = 'rgba(220,38,38,.6)';
          }};
          sw.onmessage = (ev) => {{
            if (!(ev.data instanceof ArrayBuffer)) return;
            if (lastUrl) URL.revokeObjectURL(lastUrl);
            lastUrl = URL.createObjectURL(new Blob([ev.data], {{type:'image/jpeg'}}));
            tpImg.src = lastUrl;
            tpFpsN++;
            const now = performance.now();
            if (now - tpFpsT > 1000) {{
              fpsEl.textContent = tpFpsN + ' fps';
              tpFpsN = 0; tpFpsT = now;
            }}
          }};
          // Double-tap the preview to toggle 2x zoom (cursor-following crop).
          // In zoom mode the server crops a half-screen region around the
          // mouse and scales it to the same output width, so each pixel is
          // twice as detailed — much sharper text without bigger payloads.
          let lastTap = 0, zoomed = false;
          tpImg.addEventListener('touchend', e => {{
            const now = performance.now();
            if (now - lastTap < 300) {{
              e.preventDefault();
              zoomed = !zoomed;
              if (sw.readyState === 1)
                sw.send(JSON.stringify({{m:'set', z: zoomed ? 3 : 1}}));
              tpImg.style.outline = zoomed ? '3px solid #22c55e' : '';
              badge.textContent = zoomed ? '3x' : 'live';
            }}
            lastTap = now;
          }}, {{passive:false}});
          // Wire up the stream checkbox (pause/resume via control message)
          const cb = card.querySelector('#tp-stream');
          if (cb && !cb.dataset.streamWired) {{
            cb.dataset.streamWired = '1';
            cb.addEventListener('change', () => {{
              if (sw.readyState === 1)
                sw.send(JSON.stringify({{m: cb.checked ? 'resume' : 'pause'}}));
              badge.textContent = cb.checked ? 'live' : 'paused';
              badge.style.background = cb.checked
                ? 'rgba(34,197,94,.6)' : 'rgba(91,100,115,.6)';
            }});
          }}
          wirePad(card);
        }}
        function stopStream() {{
          tpStreamOn = false;
          const card = document.querySelector('.card[data-cmd="trackpad"]');
          if (card && card._streamWs) {{
            card._streamWs.close();
            card._streamWs = null;
          }}
          if (tpImg) tpImg.src = '';
        }}
        function wirePad(card) {{
          const pad = card.querySelector('.pad');
          if (pad.dataset.wired) return;
          pad.dataset.wired = '1';
          const sens = () => parseFloat(card.querySelector('#tp-sens').value);
          // Gesture scheme:
          //   one-finger drag  = move cursor
          //   one-finger tap   = left click
          //   two-finger tap   = right click
          //   two-finger drag  = left drag (hold button, move, release)
          let touch = null;       // single-finger state
          let twoFinger = false;
          let twoStart = null;     // {{x, y, startX, startY, moved}}
          pad.addEventListener('touchstart', e => {{
            e.preventDefault();
            if (e.touches.length === 1) {{
              const t = e.touches[0];
              touch = {{x:t.clientX, y:t.clientY, startX:t.clientX,
                        startY:t.clientY, startTime:performance.now(),
                        moved:false}};
              twoFinger = false; twoStart = null;
            }} else if (e.touches.length === 2) {{
              twoFinger = true;
              touch = null;  // abandon single-finger gesture
              const t = e.touches[0];
              twoStart = {{x:t.clientX, y:t.clientY, startX:t.clientX,
                           startY:t.clientY, moved:false, dragging:false}};
            }}
          }}, {{passive:false}});
          pad.addEventListener('touchmove', e => {{
            e.preventDefault();
            if (twoFinger && twoStart) {{
              const t = e.touches[0];
              const dx = (t.clientX - twoStart.x) * sens();
              const dy = (t.clientY - twoStart.y) * sens();
              if (Math.abs(t.clientX - twoStart.startX) > 6 ||
                  Math.abs(t.clientY - twoStart.startY) > 6)
                twoStart.moved = true;
              // On first significant move, press the left button down.
              if (twoStart.moved && !twoStart.dragging) {{
                mouseCmd('mousedrag', {{action:'down', button:'left'}});
                twoStart.dragging = true;
              }}
              if (twoStart.dragging)
                mouseCmd('mousemove', {{dx:Math.round(dx), dy:Math.round(dy)}});
              twoStart.x = t.clientX;
              twoStart.y = t.clientY;
              return;
            }}
            if (!touch) return;
            const t = e.touches[0];
            const dx = (t.clientX - touch.x) * sens();
            const dy = (t.clientY - touch.y) * sens();
            if (Math.abs(t.clientX - touch.startX) > 6 ||
                Math.abs(t.clientY - touch.startY) > 6)
              touch.moved = true;
            mouseCmd('mousemove', {{dx:Math.round(dx), dy:Math.round(dy)}});
            touch.x = t.clientX;
            touch.y = t.clientY;
          }}, {{passive:false}});
          pad.addEventListener('touchend', e => {{
            if (twoFinger && twoStart) {{
              if (e.touches.length === 0) {{
                if (twoStart.dragging) {{
                  // Two-finger drag ended — release the left button.
                  mouseCmd('mousedrag', {{action:'up', button:'left'}});
                }} else if (!twoStart.moved) {{
                  // Two-finger tap — right click.
                  mouseCmd('mouseclick', {{button:'right'}});
                }}
                twoFinger = false; twoStart = null;
              }}
              return;
            }}
            if (!touch) return;
            const dt = performance.now() - touch.startTime;
            if (!touch.moved && dt < 300)
              mouseCmd('mouseclick', {{button:'left'}});
            touch = null;
          }});
          // Mouse events for desktop testing.
          let mouseDown = false, mouseLast = null;
          pad.addEventListener('mousedown', e => {{
            mouseDown = true;
            mouseLast = {{x:e.clientX, y:e.clientY}};
          }});
          pad.addEventListener('mousemove', e => {{
            if (!mouseDown || !mouseLast) return;
            const dx = (e.clientX - mouseLast.x) * sens();
            const dy = (e.clientY - mouseLast.y) * sens();
            mouseCmd('mousemove', {{dx:Math.round(dx), dy:Math.round(dy)}});
            mouseLast = {{x:e.clientX, y:e.clientY}};
          }});
          pad.addEventListener('mouseup', e => {{
            if (!mouseDown) return;
            if (e.button === 2) mouseCmd('mouseclick', {{button:'right'}});
            else if (e.button === 0) mouseCmd('mouseclick', {{button:'left'}});
            mouseDown = false; mouseLast = null;
          }});
          pad.addEventListener('contextmenu', e => e.preventDefault());
        }}
        // Low-latency mouse input over a single persistent WebSocket.
        // Every move is a ~20-byte JSON frame with no HTTP overhead, so the
        // cursor tracks the finger at network RTT (sub-millisecond on LAN)
        // instead of HTTP round-trip (~20-30ms per move).
        let ws = null, wsQueue = [];
        function wsSend(msg) {{
          if (ws && ws.readyState === 1) {{ ws.send(JSON.stringify(msg)); }}
          else {{ wsQueue.push(msg); wsConnect(); }}
        }}
        function wsConnect() {{
          if (ws && ws.readyState <= 1) return;  // connecting or open
          const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
          ws = new WebSocket(proto + '//' + location.host + '/ws?token=' + encodeURIComponent(TOKEN));
          ws.onopen = () => {{ while (wsQueue.length) ws.send(JSON.stringify(wsQueue.shift())); }};
          ws.onclose = () => {{ ws = null; }};
          ws.onerror = () => {{ ws = null; }};
        }}
        function mouseCmd(cmd, body) {{
          // Map old HTTP command names to short WS messages.
          if (cmd === 'mousemove') wsSend({{m:'move', dx:body.dx, dy:body.dy}});
          else if (cmd === 'mouseclick') wsSend({{m:'click', button:body.button}});
          else if (cmd === 'mousedrag' && body.action === 'down') wsSend({{m:'down', button:body.button||'left'}});
          else if (cmd === 'mousedrag' && body.action === 'up') wsSend({{m:'up', button:body.button||'left'}});
        }}
        function mouseBtn(b) {{ mouseCmd('mouseclick', {{button:b}}); }}
        // --- Keyboard: type text / send special keys to the focused window ---
        async function tpSendText() {{
          const inp = document.getElementById('tp-kb');
          const text = inp.value;
          if (!text) return;
          try {{
            await fetch('/type?token=' + encodeURIComponent(TOKEN),
              {{method:'POST', headers:{{'Content-Type':'application/json'}},
               body: JSON.stringify({{text: text}})}});
          }} catch (e) {{}}
          inp.value = '';
        }}
        async function tpSendKeys(combo) {{
          try {{
            await fetch('/keys?token=' + encodeURIComponent(TOKEN),
              {{method:'POST', headers:{{'Content-Type':'application/json'}},
               body: JSON.stringify({{combo: combo}})}});
          }} catch (e) {{}}
        }}
        // Wire up the keyboard input: Enter types the text, then sends Enter.
        (function() {{
          const inp = document.getElementById('tp-kb');
          if (inp && !inp.dataset.wired) {{
            inp.dataset.wired = '1';
            inp.addEventListener('keydown', (e) => {{
              if (e.key === 'Enter') {{
                e.preventDefault();
                tpSendText().then(() => tpSendKeys('enter'));
              }}
            }});
          }}
        }})();

        // --- Interactive terminal over WebSocket + ConPTY ---
        // The server spawns a real shell (powershell/bash) on a ConPTY and
        // pumps raw bytes over a WebSocket. We render the ANSI output in a
        // <pre> and send keystrokes back. This gives a real terminal with
        // prompt, colors, persistent cwd, tab-completion, and history.
        let termWs = null, termBuf = '', termScroll = null;
        function toggleTerminal(card) {{
          const t = card.querySelector('.term');
          const open = t.classList.toggle('open');
          if (open) termConnect(card); else termDisconnect();
        }}
        function termConnect(card) {{
          termDisconnect();
          const shell = card.querySelector('#term-shell').value;
          const screen = card.querySelector('#term-screen');
          const status = card.querySelector('#term-status');
          screen.textContent = '';
          termBuf = '';
          termScroll = screen;
          status.textContent = 'connecting…';
          const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
          // Estimate cols/rows from the screen width.
          const cols = Math.max(40, Math.floor(screen.clientWidth / 7));
          const rows = Math.max(10, Math.floor(screen.clientHeight / 14));
          termWs = new WebSocket(proto + '//' + location.host + '/term?token='
            + encodeURIComponent(TOKEN) + '&shell=' + shell + '&cols=' + cols + '&rows=' + rows);
          termWs.binaryType = 'arraybuffer';
          termWs.onopen = () => {{ status.textContent = 'connected'; status.style.color = '#22c55e'; }};
          termWs.onclose = () => {{ status.textContent = 'disconnected'; status.style.color = '#5b6473'; termWs = null; }};
          termWs.onerror = () => {{ status.textContent = 'error'; status.style.color = '#ef4444'; }};
          termWs.onmessage = (ev) => {{
            let data;
            if (ev.data instanceof ArrayBuffer) {{
              data = new TextDecoder().decode(ev.data);
            }} else {{
              // Text frame might be JSON error or raw text.
              try {{ const j = JSON.parse(ev.data); if (j.error) {{ status.textContent = j.error; return; }} }} catch(e) {{}}
              data = ev.data;
            }}
            termBuf += data;
            // Cap buffer to avoid unbounded growth.
            if (termBuf.length > 100000) termBuf = termBuf.slice(-80000);
            termRender(screen);
          }};
          // Focus the input when tapping the screen.
          screen.onclick = () => card.querySelector('#term-input').focus();
        }}
        function termDisconnect() {{
          if (termWs) {{ termWs.close(); termWs = null; }}
        }}
        function termReconnect(card) {{ termConnect(card); }}
        function termClear(card) {{
          termBuf = '';
          card.querySelector('#term-screen').textContent = '';
        }}
        function termRender(screen) {{
          // Render the buffer as HTML with ANSI color support.
          screen.innerHTML = ansiToHtml(termBuf, '', undefined);
          // Auto-scroll to bottom.
          screen.scrollTop = screen.scrollHeight;
        }}
        function termSendInput(card) {{
          const inp = card.querySelector('#term-input');
          const text = inp.value;
          if (!termWs || termWs.readyState !== 1) return;
          // Send as a JSON control message with the text + newline.
          termWs.send(JSON.stringify({{m:'input', data: text + '\\r'}}));
          inp.value = '';
        }}
        // Wire up the input box: Enter sends, arrow keys send escape sequences.
        function wireTermInput(card) {{
          const inp = card.querySelector('#term-input');
          if (inp.dataset.wired) return;
          inp.dataset.wired = '1';
          inp.addEventListener('keydown', (e) => {{
            if (e.key === 'Enter') {{
              e.preventDefault();
              termSendInput(card);
            }} else if (e.key === 'ArrowUp') {{
              e.preventDefault();
              if (termWs && termWs.readyState === 1) termWs.send(JSON.stringify({{m:'input', data:'\\x1b[A'}}));
            }} else if (e.key === 'ArrowDown') {{
              e.preventDefault();
              if (termWs && termWs.readyState === 1) termWs.send(JSON.stringify({{m:'input', data:'\\x1b[B'}}));
            }} else if (e.key === 'Tab') {{
              e.preventDefault();
              if (termWs && termWs.readyState === 1) termWs.send(JSON.stringify({{m:'input', data:'\\t'}}));
            }} else if (e.key === 'c' && e.ctrlKey) {{
              e.preventDefault();
              if (termWs && termWs.readyState === 1) termWs.send(JSON.stringify({{m:'input', data:'\\x03'}}));
            }}
          }});
        }}
        </script></body></html>"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
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
        # Special-case cards that need custom UI instead of the generic form.
        if name == "sendfile":
            details = ('<div class="details"><div class="file-field">'
                       '<input type="file" name="file" multiple '
                       'onchange="uploadFile(this.closest(\'.card\'))">'
                       '<div class="out"></div></div></div>')
            chev = ""
            return (f'<div class="card" data-cmd="{name}" data-live="false">'
                    f'<div class="row" onclick="toggleDetails(this.closest(\'.card\'))">'
                    f'<span class="name">{name}</span></div>{details}</div>')
        if name == "trackpad":
            details = ('<div class="trackpad">'
                       '<div class="preview"><img alt="screen"><span class="badge">'
                       'connecting…</span></div>'
                       '<div class="pad"></div>'
                       '<div class="btns"><button onclick="mouseBtn(\'left\')">Left Click</button>'
                       '<button onclick="mouseBtn(\'right\')">Right Click</button></div>'
                       '<div class="kb-row">'
                       '<input type="text" id="tp-kb" placeholder="type to send keystrokes to PC…" '
                       'autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false">'
                       '<button onclick="tpSendText()">Type</button>'
                       '</div>'
                       '<div class="kb-shortcuts">'
                       '<button onclick="tpSendKeys(\'enter\')">⏎</button>'
                       '<button onclick="tpSendKeys(\'ctrl+c\')">Copy</button>'
                       '<button onclick="tpSendKeys(\'ctrl+v\')">Paste</button>'
                       '<button onclick="tpSendKeys(\'ctrl+x\')">Cut</button>'
                       '<button onclick="tpSendKeys(\'ctrl+z\')">Undo</button>'
                       '<button onclick="tpSendKeys(\'alt+tab\')">Alt+Tab</button>'
                       '<button onclick="tpSendKeys(\'win+d\')">Win+D</button>'
                       '</div>'
                       '<div class="ctrls">'
                       '<label><input type="checkbox" id="tp-stream" checked> Stream</label>'
                       '<label>Sensitivity <input type="range" id="tp-sens" min="4.5" '
                       'max="27" step="0.9" value="13.5"></label>'
                       '<span class="fps" id="tp-fps">— fps</span>'
                       '</div>'
                       '<div class="out"></div></div>')
            chev = ""
            return (f'<div class="card" data-cmd="{name}" data-live="false">'
                    f'<div class="row" onclick="toggleTrackpad(this.closest(\'.card\'))">'
                    f'<span class="name">{name}</span>{chev}{ping_flag}'
                    f'</div>{details}{confirm_box}{undo_box}</div>')
        if name == "terminal":
            details = ('<div class="term">'
                       '<div class="bar">'
                       '<select id="term-shell"><option value="ps">PowerShell</option>'
                       '<option value="wsl">WSL bash</option></select>'
                       '<button onclick="termReconnect(this.closest(\'.card\'))">Reconnect</button>'
                       '<button onclick="termClear(this.closest(\'.card\'))">Clear</button>'
                       '<span class="status" id="term-status">disconnected</span>'
                       '</div>'
                       '<div class="screen" id="term-screen" tabindex="0"></div>'
                       '<div class="input-row">'
                       '<input type="text" id="term-input" placeholder="type a command and press Enter…" '
                       'autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false">'
                       '<button onclick="termSendInput(this.closest(\'.card\'))">Send</button>'
                       '</div>'
                       '<div class="out"></div></div>')
            chev = ""
            return (f'<div class="card" data-cmd="{name}" data-live="false">'
                    f'<div class="row" onclick="toggleTerminal(this.closest(\'.card\'))">'
                    f'<span class="name">{name}</span>{chev}{ping_flag}'
                    f'</div>{details}{confirm_box}{undo_box}</div>')
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
