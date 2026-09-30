"""Win32 plumbing via ctypes: input injection, clipboard, cursor shape,
display enumeration, audio/brightness and system stats. No dependencies
beyond the optional `winrt` packages used for master volume."""
import asyncio
import ctypes
import os
import struct
import threading
import time
import zlib
from ctypes import wintypes

# --- DPI awareness -------------------------------------------------------
# Must run before any metric/cursor call: without it Windows reports scaled
# logical coordinates (2048x1152 instead of 2560x1440 at 125%).
def _set_dpi_awareness():
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return  # PER_MONITOR_AWARE_V2
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


_set_dpi_awareness()

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

HANDLE, HWND, HDC, HBITMAP = wintypes.HANDLE, wintypes.HWND, wintypes.HDC, wintypes.HBITMAP
LPVOID = ctypes.c_void_p


def _fn(dll, name, restype, *argtypes):
    f = getattr(dll, name)
    f.restype, f.argtypes = restype, list(argtypes)
    return f


# ==========================================================================
# Input injection (SendInput)
# ==========================================================================
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, KEYEVENTF_SCANCODE = 1, 2, 4, 8
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_VIRTUALDESK = 0x1, 0x8000, 0x4000
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x800, 0x1000
_BUTTON_FLAGS = {  # name -> (down, up, mouseData)
    "left": (0x2, 0x4, 0), "right": (0x8, 0x10, 0), "middle": (0x20, 0x40, 0),
    "x1": (0x80, 0x100, 1), "x2": (0x80, 0x100, 2),
}


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUT(ctypes.Structure):
    # The union must include MOUSEINPUT (the largest member) or sizeof(INPUT)
    # is wrong and SendInput silently rejects every event.
    class _U(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


_SendInput = _fn(user32, "SendInput", wintypes.UINT, wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
_MapVirtualKeyW = _fn(user32, "MapVirtualKeyW", wintypes.UINT, wintypes.UINT, wintypes.UINT)
_GetSystemMetrics = _fn(user32, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
_GetCursorPos = _fn(user32, "GetCursorPos", wintypes.BOOL, ctypes.POINTER(wintypes.POINT))
_VkKeyScanW = _fn(user32, "VkKeyScanW", ctypes.c_short, wintypes.WCHAR)


def _send(events: list) -> None:
    if not events:
        return
    arr = (_INPUT * len(events))(*events)
    _SendInput(len(events), arr, ctypes.sizeof(_INPUT))


def _mouse_event(flags, dx=0, dy=0, data=0) -> _INPUT:
    ev = _INPUT(type=INPUT_MOUSE)
    ev.mi = _MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
    return ev


def virtual_screen() -> tuple[int, int, int, int]:
    """(x, y, w, h) of the whole virtual desktop in physical pixels."""
    return (_GetSystemMetrics(76), _GetSystemMetrics(77),
            max(1, _GetSystemMetrics(78)), max(1, _GetSystemMetrics(79)))


def cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    _GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


class Mouse:
    """Absolute-positioning mouse. The trackpad sends relative deltas that
    the client already shaped with its own acceleration curve, so we must
    NOT feed them through relative mouse_event (Windows would add its own
    'Enhance pointer precision' curve on top, and the client's cursor
    prediction would drift). We track a sub-pixel position and place the
    cursor exactly with MOUSEEVENTF_ABSOLUTE."""

    def __init__(self):
        self._lock = threading.Lock()
        self._x, self._y = map(float, cursor_pos())
        self._set = (int(self._x), int(self._y))
        self._set_at = 0.0
        self._wheel_carry = [0.0, 0.0]
        self.last_input = 0.0

    def _sync(self):
        # Adopt the real position if something else (the physical mouse, an
        # app warping the cursor) moved it since our last placement.
        ax, ay = cursor_pos()
        if (abs(ax - self._set[0]) > 1 or abs(ay - self._set[1]) > 1) and \
                time.monotonic() - self._set_at > 0.05:
            self._x, self._y = float(ax), float(ay)

    def _place(self, px: int, py: int):
        vx, vy, vw, vh = virtual_screen()
        # Normalized 0..65535 across the virtual desktop; +0.5 targets the
        # pixel centre so rounding inside Windows lands on (px, py).
        nx = int(((px - vx) + 0.5) * 65536 / vw)
        ny = int(((py - vy) + 0.5) * 65536 / vh)
        _send([_mouse_event(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                            max(0, min(65535, nx)), max(0, min(65535, ny)))])
        self._set = (px, py)
        self._set_at = time.monotonic()
        self.last_input = self._set_at

    def move_by(self, dx: float, dy: float) -> tuple[int, int]:
        with self._lock:
            self._sync()
            vx, vy, vw, vh = virtual_screen()
            self._x = min(max(self._x + dx, vx), vx + vw - 1)
            self._y = min(max(self._y + dy, vy), vy + vh - 1)
            p = (int(round(self._x)), int(round(self._y)))
            if p != self._set:
                self._place(*p)
            return p

    def move_to(self, x: float, y: float) -> tuple[int, int]:
        with self._lock:
            vx, vy, vw, vh = virtual_screen()
            self._x = min(max(x, vx), vx + vw - 1)
            self._y = min(max(y, vy), vy + vh - 1)
            p = (int(round(self._x)), int(round(self._y)))
            self._place(*p)
            return p

    def button(self, name: str, down: bool):
        down_f, up_f, data = _BUTTON_FLAGS.get(name, _BUTTON_FLAGS["left"])
        _send([_mouse_event(down_f if down else up_f, data=data)])
        self.last_input = time.monotonic()

    def click(self, name: str = "left", count: int = 1):
        down_f, up_f, data = _BUTTON_FLAGS.get(name, _BUTTON_FLAGS["left"])
        _send([_mouse_event(f, data=data) for _ in range(max(1, min(3, count))) for f in (down_f, up_f)])
        self.last_input = time.monotonic()

    def wheel(self, dy: float, dx: float = 0.0):
        """Scroll by fractional wheel units (120 = one notch). Positive dy
        scrolls up, positive dx scrolls right. Fractions are carried over so
        slow two-finger scrolls accumulate instead of being truncated away;
        modern apps treat sub-notch deltas as smooth pixel scrolling."""
        with self._lock:
            self._wheel_carry[0] += dy
            self._wheel_carry[1] += dx
            v, h = int(self._wheel_carry[0]), int(self._wheel_carry[1])
            self._wheel_carry[0] -= v
            self._wheel_carry[1] -= h
        evs = []
        if v:
            evs.append(_mouse_event(MOUSEEVENTF_WHEEL, data=v))
        if h:
            evs.append(_mouse_event(MOUSEEVENTF_HWHEEL, data=h))
        _send(evs)
        if evs:
            self.last_input = time.monotonic()


MOUSE = Mouse()

# --- keyboard -----------------------------------------------------------
VK_NAMES = {
    "ctrl": 0xA2, "control": 0xA2, "lctrl": 0xA2, "rctrl": 0xA3,
    "alt": 0xA4, "menu": 0xA4, "lalt": 0xA4, "ralt": 0xA5, "altgr": 0xA5,
    "shift": 0xA0, "lshift": 0xA0, "rshift": 0xA1,
    "win": 0x5B, "meta": 0x5B, "super": 0x5B, "cmd": 0x5B, "lwin": 0x5B, "rwin": 0x5C,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "bksp": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D,
    "space": 0x20, "spacebar": 0x20, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "pgup": 0x21, "pgdn": 0x22,
    "prtsc": 0x2C, "printscreen": 0x2C, "print": 0x2C, "scrolllock": 0x91, "pause": 0x13,
    "break": 0x13, "capslock": 0x14, "numlock": 0x90, "apps": 0x5D, "contextmenu": 0x5D,
    "volumeup": 0xAF, "volumedown": 0xAE, "mute": 0xAD, "volumemute": 0xAD,
    "playpause": 0xB3, "play": 0xB3, "nexttrack": 0xB0, "next": 0xB0,
    "prevtrack": 0xB1, "prev": 0xB1, "previous": 0xB1, "stop": 0xB2,
    "plus": 0xBB, "minus": 0xBD, "comma": 0xBC, "period": 0xBE,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
}
# KeyboardEvent.code -> VK (physical key; the PC's layout picks the glyph).
VK_CODES = {
    **{f"Key{chr(c)}": c for c in range(0x41, 0x5B)},
    **{f"Digit{i}": 0x30 + i for i in range(10)},
    **{f"Numpad{i}": 0x60 + i for i in range(10)},
    **{f"F{i}": 0x6F + i for i in range(1, 25)},
    "Enter": 0x0D, "NumpadEnter": 0x0D, "Escape": 0x1B, "Backspace": 0x08, "Tab": 0x09,
    "Space": 0x20, "Minus": 0xBD, "Equal": 0xBB, "BracketLeft": 0xDB, "BracketRight": 0xDD,
    "Backslash": 0xDC, "Semicolon": 0xBA, "Quote": 0xDE, "Backquote": 0xC0, "Comma": 0xBC,
    "Period": 0xBE, "Slash": 0xBF, "IntlBackslash": 0xE2, "CapsLock": 0x14,
    "PrintScreen": 0x2C, "ScrollLock": 0x91, "Pause": 0x13, "Insert": 0x2D, "Home": 0x24,
    "PageUp": 0x21, "Delete": 0x2E, "End": 0x23, "PageDown": 0x22, "ArrowRight": 0x27,
    "ArrowLeft": 0x25, "ArrowDown": 0x28, "ArrowUp": 0x26, "NumLock": 0x90,
    "NumpadDivide": 0x6F, "NumpadMultiply": 0x6A, "NumpadSubtract": 0x6D, "NumpadAdd": 0x6B,
    "NumpadDecimal": 0x6E, "ContextMenu": 0x5D, "ShiftLeft": 0xA0, "ShiftRight": 0xA1,
    "ControlLeft": 0xA2, "ControlRight": 0xA3, "AltLeft": 0xA4, "AltRight": 0xA5,
    "MetaLeft": 0x5B, "MetaRight": 0x5C, "OSLeft": 0x5B, "OSRight": 0x5C,
    "AudioVolumeMute": 0xAD, "AudioVolumeDown": 0xAE, "AudioVolumeUp": 0xAF,
    "MediaTrackNext": 0xB0, "MediaTrackPrevious": 0xB1, "MediaStop": 0xB2, "MediaPlayPause": 0xB3,
}
# Keys that live on the extended (E0) scan-code set. Without the flag,
# arrows/Home/End/etc. are delivered as their numpad twins.
_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2C, 0x2D, 0x2E, 0x5B, 0x5C,
             0x5D, 0x6F, 0x90, 0xA3, 0xA5, 0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3}
MODIFIER_VKS = {0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C, 0x10, 0x11, 0x12}


def _key_event(vk: int, up: bool, extended: bool | None = None) -> _INPUT:
    ev = _INPUT(type=INPUT_KEYBOARD)
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in _EXTENDED if extended is None else extended:
        flags |= KEYEVENTF_EXTENDEDKEY
    ev.ki = _KEYBDINPUT(vk, _MapVirtualKeyW(vk, 0) & 0xFF, flags, 0, 0)
    return ev


def key(vk: int, down: bool, extended: bool | None = None):
    _send([_key_event(vk, not down, extended)])
    MOUSE.last_input = time.monotonic()


def parse_combo(combo: str) -> list[int]:
    """'ctrl+shift+t' -> [VK_LCONTROL, VK_LSHIFT, 'T']. Raises ValueError."""
    vks = []
    for part in (p.strip().lower() for p in combo.replace(" ", "").split("+")):
        if not part:
            continue
        if part in VK_NAMES:
            vks.append(VK_NAMES[part])
        elif len(part) == 1 and part.isalnum():
            vks.append(ord(part.upper()))
        elif len(part) == 1:
            r = _VkKeyScanW(part)
            if r == -1 or r & 0xFF == 0xFF:
                raise ValueError(f"unknown key: {part}")
            vks.append(r & 0xFF)
        else:
            raise ValueError(f"unknown key: {part}")
    if not vks:
        raise ValueError("empty combo")
    return vks


def send_combo(combo: str):
    """Press modifiers + key in order, release in reverse: 'win+d', 'f5'."""
    vks = parse_combo(combo)
    _send([_key_event(v, False) for v in vks] + [_key_event(v, True) for v in reversed(vks)])
    MOUSE.last_input = time.monotonic()


def type_text(text: str):
    """Type Unicode text into the focused window. Newlines/tabs become real
    Enter/Tab presses (many apps ignore the raw control characters) and
    non-BMP characters (emoji) are sent as UTF-16 surrogate pairs."""
    evs = []
    for ch in text.replace("\r\n", "\n"):
        if ch in "\n\r":
            evs += [_key_event(0x0D, False), _key_event(0x0D, True)]
        elif ch == "\t":
            evs += [_key_event(0x09, False), _key_event(0x09, True)]
        elif ch == "\b":
            evs += [_key_event(0x08, False), _key_event(0x08, True)]
        else:
            units = ch.encode("utf-16-le")
            for i in range(0, len(units), 2):
                code = units[i] | (units[i + 1] << 8)
                for up in (False, True):
                    ev = _INPUT(type=INPUT_KEYBOARD)
                    ev.ki = _KEYBDINPUT(0, code, KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0), 0, 0)
                    evs.append(ev)
    for i in range(0, len(evs), 200):  # keep single SendInput batches modest
        _send(evs[i:i + 200])
    MOUSE.last_input = time.monotonic()


def release_keys(vks):
    _send([_key_event(v, True) for v in vks])


# ==========================================================================
# Clipboard (Unicode text)
# ==========================================================================
CF_UNICODETEXT = 13
_OpenClipboard = _fn(user32, "OpenClipboard", wintypes.BOOL, HWND)
_CloseClipboard = _fn(user32, "CloseClipboard", wintypes.BOOL)
_EmptyClipboard = _fn(user32, "EmptyClipboard", wintypes.BOOL)
_GetClipboardData = _fn(user32, "GetClipboardData", HANDLE, wintypes.UINT)
_SetClipboardData = _fn(user32, "SetClipboardData", HANDLE, wintypes.UINT, HANDLE)
_GetClipboardSequenceNumber = _fn(user32, "GetClipboardSequenceNumber", wintypes.DWORD)
_GlobalAlloc = _fn(kernel32, "GlobalAlloc", HANDLE, wintypes.UINT, ctypes.c_size_t)
_GlobalFree = _fn(kernel32, "GlobalFree", HANDLE, HANDLE)
_GlobalLock = _fn(kernel32, "GlobalLock", LPVOID, HANDLE)
_GlobalUnlock = _fn(kernel32, "GlobalUnlock", wintypes.BOOL, HANDLE)
_CreateWindowExW = _fn(user32, "CreateWindowExW", HWND, wintypes.DWORD, wintypes.LPCWSTR,
                       wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                       ctypes.c_int, HWND, HANDLE, HANDLE, LPVOID)

_clip_lock = threading.Lock()
_clip_hwnd = None


def _open_clipboard() -> bool:
    # EmptyClipboard with a NULL owner makes SetClipboardData fail, so we
    # own a hidden message-only window. Another app may briefly hold the
    # clipboard open; retry for ~200ms.
    global _clip_hwnd
    if _clip_hwnd is None:
        _clip_hwnd = _CreateWindowExW(0, "STATIC", "pc-remote-clipboard", 0, 0, 0, 0, 0,
                                      HWND(-3), None, None, None) or 0
    for _ in range(20):
        if _OpenClipboard(_clip_hwnd or None):
            return True
        time.sleep(0.01)
    return False


def clipboard_seq() -> int:
    return _GetClipboardSequenceNumber()


def get_clipboard() -> str:
    with _clip_lock:
        if not _open_clipboard():
            raise OSError("clipboard is busy")
        try:
            h = _GetClipboardData(CF_UNICODETEXT)
            if not h:
                return ""
            p = _GlobalLock(h)
            if not p:
                return ""
            try:
                return ctypes.wstring_at(p)
            finally:
                _GlobalUnlock(h)
        finally:
            _CloseClipboard()


def set_clipboard(text: str):
    data = text.encode("utf-16-le") + b"\x00\x00"
    with _clip_lock:
        if not _open_clipboard():
            raise OSError("clipboard is busy")
        try:
            _EmptyClipboard()
            h = _GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
            p = _GlobalLock(h)
            ctypes.memmove(p, data, len(data))
            _GlobalUnlock(h)
            if not _SetClipboardData(CF_UNICODETEXT, h):
                _GlobalFree(h)
                raise OSError("SetClipboardData failed")
        finally:
            _CloseClipboard()


# ==========================================================================
# Cursor state + shape (rendered client-side over the video)
# ==========================================================================
class _CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hCursor", HANDLE), ("ptScreenPos", wintypes.POINT)]


class _ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
                ("hbmMask", HBITMAP), ("hbmColor", HBITMAP)]


class _BITMAP(ctypes.Structure):
    _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG), ("bmHeight", wintypes.LONG),
                ("bmWidthBytes", wintypes.LONG), ("bmPlanes", wintypes.WORD),
                ("bmBitsPixel", wintypes.WORD), ("bmBits", LPVOID)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


_GetCursorInfo = _fn(user32, "GetCursorInfo", wintypes.BOOL, ctypes.POINTER(_CURSORINFO))
_GetIconInfo = _fn(user32, "GetIconInfo", wintypes.BOOL, HANDLE, ctypes.POINTER(_ICONINFO))
_DrawIconEx = _fn(user32, "DrawIconEx", wintypes.BOOL, HDC, ctypes.c_int, ctypes.c_int, HANDLE,
                  ctypes.c_int, ctypes.c_int, wintypes.UINT, HANDLE, wintypes.UINT)
_GetDC = _fn(user32, "GetDC", HDC, HWND)
_ReleaseDC = _fn(user32, "ReleaseDC", ctypes.c_int, HWND, HDC)
_GetObjectW = _fn(gdi32, "GetObjectW", ctypes.c_int, HANDLE, ctypes.c_int, LPVOID)
_CreateCompatibleDC = _fn(gdi32, "CreateCompatibleDC", HDC, HDC)
_DeleteDC = _fn(gdi32, "DeleteDC", wintypes.BOOL, HDC)
_DeleteObject = _fn(gdi32, "DeleteObject", wintypes.BOOL, HANDLE)
_SelectObject = _fn(gdi32, "SelectObject", HANDLE, HDC, HANDLE)
_CreateDIBSection = _fn(gdi32, "CreateDIBSection", HBITMAP, HDC, ctypes.POINTER(BITMAPINFOHEADER),
                        wintypes.UINT, ctypes.POINTER(LPVOID), HANDLE, wintypes.DWORD)
_BitBlt = _fn(gdi32, "BitBlt", wintypes.BOOL, HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
              ctypes.c_int, HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD)
_GdiFlush = _fn(gdi32, "GdiFlush", wintypes.BOOL)


def cursor_state() -> tuple[bool, int, int, int]:
    """(visible, x, y, hcursor) in virtual-desktop physical pixels."""
    ci = _CURSORINFO(cbSize=ctypes.sizeof(_CURSORINFO))
    if not _GetCursorInfo(ctypes.byref(ci)):
        x, y = cursor_pos()
        return False, x, y, 0
    return bool(ci.flags & 1) and bool(ci.hCursor), ci.ptScreenPos.x, ci.ptScreenPos.y, ci.hCursor or 0


class _DIB:
    """A top-down 32bpp DIB section selected into a memory DC."""
    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        bi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w, biHeight=-h,
                              biPlanes=1, biBitCount=32)
        self.bits = LPVOID()
        screen = _GetDC(None)
        self.dc = _CreateCompatibleDC(screen)
        _ReleaseDC(None, screen)
        self.bmp = _CreateDIBSection(self.dc, ctypes.byref(bi), 0, ctypes.byref(self.bits), None, 0)
        if not self.bmp or not self.bits:
            _DeleteDC(self.dc)
            raise OSError("CreateDIBSection failed")
        self._old = _SelectObject(self.dc, self.bmp)

    def fill(self, byte: int):
        ctypes.memset(self.bits, byte, self.w * self.h * 4)

    def read(self) -> bytes:
        _GdiFlush()
        return ctypes.string_at(self.bits, self.w * self.h * 4)

    def close(self):
        _SelectObject(self.dc, self._old)
        _DeleteObject(self.bmp)
        _DeleteDC(self.dc)


def cursor_image(hcursor: int):
    """Render a cursor to RGBA. Returns (w, h, hot_x, hot_y, rgba) or None.

    The cursor is drawn twice, over black and over white; the difference
    recovers per-pixel alpha, which handles 32-bit alpha cursors, classic
    AND/XOR monochrome cursors, and inverting pixels (the I-beam) alike.
    Inverting pixels have no fixed colour, so they're drawn dark with a
    light halo to stay visible over any background."""
    ii = _ICONINFO()
    if not hcursor or not _GetIconInfo(hcursor, ctypes.byref(ii)):
        return None
    try:
        bm = _BITMAP()
        src = ii.hbmColor or ii.hbmMask
        if not src or not _GetObjectW(src, ctypes.sizeof(bm), ctypes.byref(bm)):
            return None
        w, h = bm.bmWidth, bm.bmHeight if ii.hbmColor else bm.bmHeight // 2
    finally:
        for b in (ii.hbmMask, ii.hbmColor):
            if b:
                _DeleteObject(b)
    if not (0 < w <= 256 and 0 < h <= 256):
        return None
    shots = []
    for bg in (0x00, 0xFF):
        dib = _DIB(w, h)
        try:
            dib.fill(bg)
            _DrawIconEx(dib.dc, 0, 0, hcursor, w, h, 0, None, 0x0003)  # DI_NORMAL
            shots.append(dib.read())
        finally:
            dib.close()
    black, white = shots
    out = bytearray(w * h * 4)
    inverted = []
    for i in range(0, w * h * 4, 4):
        bb, bgc, br = black[i], black[i + 1], black[i + 2]
        wb, wg, wr = white[i], white[i + 1], white[i + 2]
        if wb < bb or wg < bgc or wr < br:          # XOR pixel: inverts
            out[i:i + 4] = b"\x10\x10\x10\xff"
            inverted.append(i // 4)
            continue
        a = 255 - ((wb - bb) + (wg - bgc) + (wr - br)) // 3
        if a <= 0:
            continue
        out[i] = min(255, br * 255 // a)
        out[i + 1] = min(255, bgc * 255 // a)
        out[i + 2] = min(255, bb * 255 // a)
        out[i + 3] = a
    for p in inverted:  # light halo around inverting strokes
        x, y = p % w, p // w
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < w and 0 <= ny < h:
                j = (ny * w + nx) * 4
                if out[j + 3] == 0:
                    out[j:j + 4] = b"\xf0\xf0\xf0\xd0"
    return w, h, ii.xHotspot, ii.yHotspot, bytes(out)


def png_encode(w: int, h: int, pixels: bytes, channels: int = 4, level: int = 6) -> bytes:
    """Minimal PNG writer (RGB or RGBA, 8-bit)."""
    stride = w * channels
    raw = b"".join(b"\x00" + pixels[y * stride:(y + 1) * stride] for y in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    ctype = 6 if channels == 4 else 2
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, level)) + chunk(b"IEND", b""))


def screenshot_png(rect: tuple[int, int, int, int] | None = None, with_cursor: bool = True) -> bytes:
    """Capture a desktop rect (default: primary display) as PNG, in-process
    so it inherits our DPI awareness (a PowerShell child would not)."""
    if rect is None:
        rect = (0, 0, _GetSystemMetrics(0), _GetSystemMetrics(1))
    x, y, w, h = rect
    dib = _DIB(w, h)
    try:
        screen = _GetDC(None)
        _BitBlt(dib.dc, 0, 0, w, h, screen, x, y, 0x00CC0020)  # SRCCOPY
        _ReleaseDC(None, screen)
        if with_cursor:
            vis, cx, cy, hc = cursor_state()
            ii = _ICONINFO()
            if vis and _GetIconInfo(hc, ctypes.byref(ii)):
                for b in (ii.hbmMask, ii.hbmColor):
                    if b:
                        _DeleteObject(b)
                _DrawIconEx(dib.dc, cx - x - ii.xHotspot, cy - y - ii.yHotspot, hc, 0, 0, 0, None, 0x0003)
        bgra = bytearray(dib.read())
    finally:
        dib.close()
    rgb = bytearray(w * h * 3)  # BGRA -> RGB with C-speed slice copies
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    return png_encode(w, h, bytes(rgb), channels=3, level=3)


# ==========================================================================
# Displays (DXGI outputs, as ffmpeg's ddagrab indexes them)
# ==========================================================================
class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, s: str):
        import uuid
        u = uuid.UUID(s)
        return cls(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))


class _DXGI_OUTPUT_DESC(ctypes.Structure):
    _fields_ = [("DeviceName", ctypes.c_wchar * 32), ("DesktopCoordinates", wintypes.RECT),
                ("AttachedToDesktop", wintypes.BOOL), ("Rotation", wintypes.UINT),
                ("Monitor", HANDLE)]


def _com(ptr, index, *args, argtypes=()):
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(LPVOID)))[0]
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, LPVOID, *argtypes)
    return proto(vtbl[index])(ptr, *args)


class _DEVMODEW(ctypes.Structure):
    _fields_ = [("dmDeviceName", ctypes.c_wchar * 32), ("dmSpecVersion", wintypes.WORD),
                ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD),
                ("dmDriverExtra", wintypes.WORD), ("dmFields", wintypes.DWORD),
                ("dmUnion1", ctypes.c_byte * 16), ("dmColor", ctypes.c_short),
                ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short),
                ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short),
                ("dmFormName", ctypes.c_wchar * 32), ("dmLogPixels", wintypes.WORD),
                ("dmBitsPerPel", wintypes.DWORD), ("dmPelsWidth", wintypes.DWORD),
                ("dmPelsHeight", wintypes.DWORD), ("dmDisplayFlags", wintypes.DWORD),
                ("dmDisplayFrequency", wintypes.DWORD), ("dmICMMethod", wintypes.DWORD),
                ("dmICMIntent", wintypes.DWORD), ("dmMediaType", wintypes.DWORD),
                ("dmDitherType", wintypes.DWORD), ("dmReserved1", wintypes.DWORD),
                ("dmReserved2", wintypes.DWORD), ("dmPanningWidth", wintypes.DWORD),
                ("dmPanningHeight", wintypes.DWORD)]


def _refresh_rate(device: str) -> int:
    dm = _DEVMODEW()
    dm.dmSize = ctypes.sizeof(_DEVMODEW)
    if user32.EnumDisplaySettingsW(device, -1, ctypes.byref(dm)):  # ENUM_CURRENT_SETTINGS
        return int(dm.dmDisplayFrequency) if dm.dmDisplayFrequency > 1 else 60
    return 60


def displays() -> list[dict]:
    """Enumerate desktop-attached outputs as {adapter, output, name, x, y, w,
    h, hz, primary}; (adapter, output) are what ddagrab/d3d11va expect."""
    out = []
    factory = LPVOID()
    iid = _GUID.parse("770aae78-f26f-4dba-a829-253c83d1b387")  # IDXGIFactory1
    try:
        dxgi = ctypes.WinDLL("dxgi")
        if dxgi.CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(factory)) != 0:
            raise OSError
    except OSError:
        x, y, w, h = 0, 0, _GetSystemMetrics(0), _GetSystemMetrics(1)
        return [{"adapter": 0, "output": 0, "name": "Display 1", "x": x, "y": y, "w": w, "h": h,
                 "hz": 60, "primary": True}]
    try:
        a = 0
        while True:
            adapter = LPVOID()
            if _com(factory, 12, a, ctypes.byref(adapter), argtypes=(wintypes.UINT, ctypes.POINTER(LPVOID))) != 0:
                break  # EnumAdapters1 -> DXGI_ERROR_NOT_FOUND
            try:
                o = 0
                while True:
                    output = LPVOID()
                    if _com(adapter, 7, o, ctypes.byref(output), argtypes=(wintypes.UINT, ctypes.POINTER(LPVOID))) != 0:
                        break
                    try:
                        d = _DXGI_OUTPUT_DESC()
                        _com(output, 7, ctypes.byref(d), argtypes=(ctypes.POINTER(_DXGI_OUTPUT_DESC),))
                        r = d.DesktopCoordinates
                        if d.AttachedToDesktop:
                            out.append({"adapter": a, "output": o, "device": d.DeviceName,
                                        "x": r.left, "y": r.top, "w": r.right - r.left,
                                        "h": r.bottom - r.top, "hz": _refresh_rate(d.DeviceName),
                                        "primary": r.left == 0 and r.top == 0})
                    finally:
                        _com(output, 2)
                    o += 1
            finally:
                _com(adapter, 2)
            a += 1
    finally:
        _com(factory, 2)
    out.sort(key=lambda d: (not d["primary"], d["x"], d["y"]))
    for i, d in enumerate(out):
        d["name"] = f"Display {i + 1}"
    return out or [{"adapter": 0, "output": 0, "name": "Display 1", "x": 0, "y": 0,
                    "w": _GetSystemMetrics(0), "h": _GetSystemMetrics(1), "hz": 60, "primary": True}]


# ==========================================================================
# Audio + brightness (coalescing workers for live sliders)
# ==========================================================================
class CoalescingSetter:
    """A worker thread that owns a (possibly thread-affine) hardware handle
    and applies only the newest pending value. A slider drag fires far more
    requests than the hardware can apply; FIFO would lag behind the finger,
    so superseded values are skipped and their callers released at once."""

    def __init__(self, name: str):
        self._name = name
        self._cond = threading.Condition()
        self._pending = None
        self._waiters: list = []
        self._gets: list = []
        self._thread = None
        self._ready = False
        self._setup_err = None

    def setup(self): ...
    def apply(self, value): raise NotImplementedError
    def read(self): return None

    def _worker(self):
        try:
            self.setup()
        except Exception as exc:  # noqa: BLE001 - surfaced to callers
            self._setup_err = repr(exc)
        with self._cond:
            self._ready = True
            self._cond.notify_all()
        if self._setup_err:
            return
        while True:
            with self._cond:
                while self._pending is None and not self._gets:
                    self._cond.wait()
                value, self._pending = self._pending, None
                waiters, self._waiters = self._waiters, []
                gets, self._gets = self._gets, []
            for ev, box in gets:
                try:
                    box[0] = ("ok", self.read())
                except Exception as exc:  # noqa: BLE001
                    box[0] = ("err", repr(exc))
                ev.set()
            if value is not None:
                err = None
                try:
                    self.apply(value)
                except Exception as exc:  # noqa: BLE001
                    err = repr(exc)
                for w_value, ev, box in waiters:
                    box[0] = err if w_value is value else None
                    ev.set()

    def _ensure(self):
        if self._thread is None or not self._thread.is_alive():
            if self._ready and self._setup_err:
                raise RuntimeError(f"{self._name}: {self._setup_err}")
            self._thread = threading.Thread(target=self._worker, name=self._name, daemon=True)
            self._thread.start()
        with self._cond:
            while not self._ready:
                self._cond.wait()
        if self._setup_err:
            raise RuntimeError(f"{self._name}: {self._setup_err}")

    def set(self, value):
        self._ensure()
        ev, box = threading.Event(), [None]
        with self._cond:
            self._pending = value
            self._waiters.append((value, ev, box))
            self._cond.notify_all()
        ev.wait(10)
        if box[0]:
            raise RuntimeError(box[0])

    def get(self):
        self._ensure()
        ev, box = threading.Event(), [("err", "timeout")]
        with self._cond:
            self._gets.append((ev, box))
            self._cond.notify_all()
        ev.wait(10)
        status, detail = box[0]
        if status == "err":
            raise RuntimeError(detail)
        return detail


class _Volume(CoalescingSetter):
    # Core Audio COM (IMMDeviceEnumerator) isn't registered on this machine
    # and winmm's mixer doesn't move WASAPI volume; the UWP
    # AudioDeviceController from a MediaCapture bound to the default render
    # endpoint does. It is STA-bound, hence the dedicated worker thread.
    def setup(self):
        import winrt.runtime
        winrt.runtime.init_apartment(winrt.runtime.ApartmentType.SINGLE_THREADED)
        import winrt.windows.media.capture as capture
        import winrt.windows.media.devices as devices
        loop = asyncio.new_event_loop()
        settings = capture.MediaCaptureInitializationSettings()
        settings.audio_device_id = devices.MediaDevice.get_default_audio_render_id(
            devices.AudioDeviceRole.DEFAULT)
        settings.streaming_capture_mode = capture.StreamingCaptureMode.AUDIO
        self._mc = capture.MediaCapture()  # must outlive the controller
        loop.run_until_complete(self._mc.initialize_with_settings_async(settings))
        self._ctrl = self._mc.audio_device_controller

    def apply(self, value):
        if isinstance(value, tuple):  # ("mute", bool)
            self._ctrl.muted = bool(value[1])
        else:
            self._ctrl.volume_percent = max(0.0, min(100.0, float(value)))

    def read(self):
        return {"level": int(round(self._ctrl.volume_percent)), "muted": bool(self._ctrl.muted)}


class _PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [("hPhysicalMonitor", HANDLE), ("szPhysicalMonitorDescription", ctypes.c_wchar * 128)]


class _Brightness(CoalescingSetter):
    # WMI brightness isn't supported on desktop monitors; DDC/CI via dxva2 is.
    def setup(self):
        d = ctypes.WinDLL("dxva2")
        self._n = _fn(d, "GetNumberOfPhysicalMonitorsFromHMONITOR", wintypes.BOOL, HANDLE, ctypes.POINTER(wintypes.DWORD))
        self._get_pm = _fn(d, "GetPhysicalMonitorsFromHMONITOR", wintypes.BOOL, HANDLE, wintypes.DWORD, ctypes.POINTER(_PHYSICAL_MONITOR))
        self._destroy = _fn(d, "DestroyPhysicalMonitors", wintypes.BOOL, wintypes.DWORD, ctypes.POINTER(_PHYSICAL_MONITOR))
        self._set = _fn(d, "SetMonitorBrightness", wintypes.BOOL, HANDLE, wintypes.DWORD)
        self._getb = _fn(d, "GetMonitorBrightness", wintypes.BOOL, HANDLE, ctypes.POINTER(wintypes.DWORD),
                         ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD))

    def _monitors(self):
        hmon = _fn(user32, "MonitorFromPoint", HANDLE, wintypes.POINT, wintypes.DWORD)(wintypes.POINT(0, 0), 1)
        n = wintypes.DWORD()
        if not hmon or not self._n(hmon, ctypes.byref(n)) or not n.value:
            raise RuntimeError("no DDC/CI monitor")
        pm = (_PHYSICAL_MONITOR * n.value)()
        if not self._get_pm(hmon, n.value, pm):
            raise RuntimeError("no DDC/CI monitor")
        return n.value, pm

    def apply(self, value):
        n, pm = self._monitors()
        try:
            ok = any([self._set(m.hPhysicalMonitor, max(0, min(100, int(value)))) for m in pm])
            if not ok:
                raise RuntimeError("DDC/CI brightness not supported (enable DDC/CI in the monitor OSD)")
        finally:
            self._destroy(n, pm)

    def read(self):
        n, pm = self._monitors()
        try:
            lo, cur, hi = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
            if self._getb(pm[0].hPhysicalMonitor, ctypes.byref(lo), ctypes.byref(cur), ctypes.byref(hi)):
                return int(cur.value)
            return None
        finally:
            self._destroy(n, pm)


VOLUME = _Volume("volume")
BRIGHTNESS = _Brightness("brightness")


# ==========================================================================
# System stats
# ==========================================================================
class _FILETIME(ctypes.Structure):
    _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]

    def value(self):
        return (self.hi << 32) | self.lo


class _MEMSTATUS(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64)]


class SystemStats:
    """CPU/RAM via kernel32, GPU via NVML when an NVIDIA driver is present."""

    def __init__(self):
        self._prev = None
        self._lock = threading.Lock()
        self._nvml = None
        self._gpu = None
        try:
            nv = ctypes.WinDLL(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvml.dll"))
            if nv.nvmlInit_v2() == 0:
                dev = LPVOID()
                if nv.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev)) == 0:
                    self._nvml, self._gpu = nv, dev
                    name = ctypes.create_string_buffer(96)
                    nv.nvmlDeviceGetName(dev, name, 96)
                    self.gpu_name = name.value.decode(errors="replace")
        except OSError:
            pass

    def _cpu(self) -> float | None:
        idle, kern, usr = _FILETIME(), _FILETIME(), _FILETIME()
        kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(usr))
        cur = (idle.value(), kern.value() + usr.value())
        with self._lock:
            prev, self._prev = self._prev, cur
        if not prev or cur[1] == prev[1]:
            return None
        return round(100.0 * (1 - (cur[0] - prev[0]) / (cur[1] - prev[1])), 1)

    def snapshot(self) -> dict:
        ms = _MEMSTATUS(dwLength=ctypes.sizeof(_MEMSTATUS))
        kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
        s = {"cpu": self._cpu(), "ram": ms.dwMemoryLoad,
             "ram_used_gb": round((ms.ullTotalPhys - ms.ullAvailPhys) / 2**30, 1),
             "ram_total_gb": round(ms.ullTotalPhys / 2**30, 1),
             "uptime_s": kernel32.GetTickCount64() // 1000}
        if self._nvml:
            class Util(ctypes.Structure):
                _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]
            u, t, enc, period = Util(), ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint()
            if self._nvml.nvmlDeviceGetUtilizationRates(self._gpu, ctypes.byref(u)) == 0:
                s["gpu"] = u.gpu
            if self._nvml.nvmlDeviceGetTemperature(self._gpu, 0, ctypes.byref(t)) == 0:
                s["gpu_temp"] = t.value
            if self._nvml.nvmlDeviceGetEncoderUtilization(self._gpu, ctypes.byref(enc), ctypes.byref(period)) == 0:
                s["nvenc"] = enc.value
            s["gpu_name"] = self.gpu_name
        return s


kernel32.GetTickCount64.restype = ctypes.c_uint64
STATS = SystemStats()


# ==========================================================================
# App icons (for the Open app / Running apps lists)
# ==========================================================================
class _SHFILEINFOW(ctypes.Structure):
    _fields_ = [("hIcon", HANDLE), ("iIcon", ctypes.c_int), ("dwAttributes", wintypes.DWORD),
                ("szDisplayName", ctypes.c_wchar * 260), ("szTypeName", ctypes.c_wchar * 80)]


_shell32 = ctypes.WinDLL("shell32")
_shell32.SHGetFileInfoW.restype = ctypes.c_void_p
_shell32.SHGetFileInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(_SHFILEINFOW), wintypes.UINT, wintypes.UINT]
_ole32 = ctypes.WinDLL("ole32")
_DestroyIcon = _fn(user32, "DestroyIcon", wintypes.BOOL, HANDLE)
_OpenProcess = _fn(kernel32, "OpenProcess", HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_QueryFullProcessImageNameW = _fn(kernel32, "QueryFullProcessImageNameW", wintypes.BOOL, HANDLE, wintypes.DWORD,
                                  wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
_CloseHandle = _fn(kernel32, "CloseHandle", wintypes.BOOL, HANDLE)


def file_icon_png(path: str) -> bytes | None:
    """The icon Explorer shows for a file or shortcut, 48x48 PNG with alpha."""
    _ole32.CoInitializeEx(None, 2)  # the shell wants COM on this thread (no-op if it's there)
    info = _SHFILEINFOW()
    if not _shell32.SHGetFileInfoW(path, 0, ctypes.byref(info), ctypes.sizeof(info), 0x4000):  # SHGFI_SYSICONINDEX
        return None
    il = LPVOID()
    iid = _GUID.parse("46eb5926-582e-4017-9fdf-e8998daa0950")  # IImageList
    if _shell32.SHGetImageList(2, ctypes.byref(iid), ctypes.byref(il)) != 0 or not il:  # SHIL_EXTRALARGE: 48 px
        return None
    icon = HANDLE()
    try:
        if _com(il, 10, info.iIcon, 1, ctypes.byref(icon),  # IImageList::GetIcon, ILD_TRANSPARENT
                argtypes=(ctypes.c_int, wintypes.UINT, ctypes.POINTER(HANDLE))) != 0 or not icon:
            return None
    finally:
        _com(il, 2)
    try:
        img = cursor_image(icon.value)  # icons and cursors are the same kind of handle
    finally:
        _DestroyIcon(icon)
    return png_encode(img[0], img[1], img[4]) if img else None


def process_path(pid: int) -> str | None:
    """Full path of a running process's program."""
    h = _OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        buf, n = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
        return buf.value if _QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else None
    finally:
        _CloseHandle(h)


_ProcessIdToSessionId = _fn(kernel32, "ProcessIdToSessionId", wintypes.BOOL, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))


def process_session(pid: int) -> int | None:
    """The sign-in session a process runs in (0: services)."""
    s = wintypes.DWORD()
    return s.value if _ProcessIdToSessionId(pid, ctypes.byref(s)) else None


_GetExtendedTcpTable = _fn(ctypes.WinDLL("iphlpapi"), "GetExtendedTcpTable", wintypes.DWORD, ctypes.c_void_p,
                           ctypes.POINTER(wintypes.DWORD), wintypes.BOOL, wintypes.ULONG, ctypes.c_int, wintypes.ULONG)


def tcp_owner(src: str, sport: int, dst: str, dport: int) -> int | None:
    """The process that owns the src:sport -> dst:dport end of a TCP
    connection, if that end is a socket on this PC (not, say, WSL or a VM
    behind this PC's address)."""
    import ipaddress
    s, d = ipaddress.ip_address(src), ipaddress.ip_address(dst)
    v6 = s.version == 6
    size, buf = wintypes.DWORD(0), None
    for _ in range(4):  # (the table can grow between the size query and the read)
        buf = ctypes.create_string_buffer(max(size.value, 4))
        r = _GetExtendedTcpTable(buf, ctypes.byref(size), False, 23 if v6 else 2, 5, 0)  # TCP_TABLE_OWNER_PID_ALL
        if r == 0:
            break
        if r != 122:  # ERROR_INSUFFICIENT_BUFFER
            return None
    else:
        return None
    port = lambda p: ((p & 0xFF) << 8) | ((p >> 8) & 0xFF)  # network byte order in the low 16 bits
    for i in range(struct.unpack_from("<I", buf, 0)[0]):
        if v6:  # MIB_TCP6ROW_OWNER_PID
            la, _, lp, ra, _, rp, _, pid = struct.unpack_from("<16sII16sIIII", buf, 4 + i * 56)
        else:  # MIB_TCPROW_OWNER_PID
            _, la, lp, ra, rp, pid = struct.unpack_from("<6I", buf, 4 + i * 24)
            la, ra = struct.pack("<I", la), struct.pack("<I", ra)
        if la == s.packed and port(lp) == sport and ra == d.packed and port(rp) == dport:
            return pid
    return None
