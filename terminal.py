"""Interactive shells over ConPTY (Windows pseudo-console).

Sessions outlive their WebSocket: switching tabs, locking the phone or a
flaky Wi-Fi reconnect re-attaches to the same shell and replays recent
output, so a long-running command keeps going. Idle detached sessions are
reaped after 30 minutes.

WS protocol (/term?sid=&shell=&cols=&rows=):
  client -> server: binary = raw input bytes (UTF-8 / VT sequences)
                    text {t:'resize', cols, rows} | {t:'restart'} | {t:'kill'}
  server -> client: binary = raw output bytes
                    text {t:'ready', sid, shell, name, replay} | {t:'exit', code}
"""
import ctypes
import os
import secrets
import shutil
import threading
import time
from ctypes import wintypes

from wsock import Outbox

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
HANDLE, DWORD, BOOL, LPVOID = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.c_void_p


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR), ("dwX", DWORD), ("dwY", DWORD), ("dwXSize", DWORD),
                ("dwYSize", DWORD), ("dwXCountChars", DWORD), ("dwYCountChars", DWORD),
                ("dwFillAttribute", DWORD), ("dwFlags", DWORD), ("wShowWindow", wintypes.WORD),
                ("cbReserved2", wintypes.WORD), ("lpReserved2", LPVOID), ("hStdInput", HANDLE),
                ("hStdOutput", HANDLE), ("hStdError", HANDLE)]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", LPVOID)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE), ("dwProcessId", DWORD), ("dwThreadId", DWORD)]


def _fn(name, restype, *argtypes):
    f = getattr(k32, name)
    f.restype, f.argtypes = restype, list(argtypes)
    return f


CreatePipe = _fn("CreatePipe", BOOL, ctypes.POINTER(HANDLE), ctypes.POINTER(HANDLE), LPVOID, DWORD)
CloseHandle = _fn("CloseHandle", BOOL, HANDLE)
ReadFile = _fn("ReadFile", BOOL, HANDLE, LPVOID, DWORD, ctypes.POINTER(DWORD), LPVOID)
WriteFile = _fn("WriteFile", BOOL, HANDLE, LPVOID, DWORD, ctypes.POINTER(DWORD), LPVOID)
CreatePseudoConsole = _fn("CreatePseudoConsole", ctypes.c_long, COORD, HANDLE, HANDLE, DWORD, ctypes.POINTER(LPVOID))
ResizePseudoConsole = _fn("ResizePseudoConsole", ctypes.c_long, LPVOID, COORD)
ClosePseudoConsole = _fn("ClosePseudoConsole", None, LPVOID)
InitializeProcThreadAttributeList = _fn("InitializeProcThreadAttributeList", BOOL, LPVOID, DWORD, DWORD, ctypes.POINTER(ctypes.c_size_t))
UpdateProcThreadAttribute = _fn("UpdateProcThreadAttribute", BOOL, LPVOID, DWORD, ctypes.c_size_t, LPVOID, ctypes.c_size_t, LPVOID, LPVOID)
DeleteProcThreadAttributeList = _fn("DeleteProcThreadAttributeList", None, LPVOID)
CreateProcessW = _fn("CreateProcessW", BOOL, wintypes.LPCWSTR, wintypes.LPWSTR, LPVOID, LPVOID, BOOL,
                     DWORD, LPVOID, wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOEXW), ctypes.POINTER(PROCESS_INFORMATION))
WaitForSingleObject = _fn("WaitForSingleObject", DWORD, HANDLE, DWORD)
GetExitCodeProcess = _fn("GetExitCodeProcess", BOOL, HANDLE, ctypes.POINTER(DWORD))
TerminateProcess = _fn("TerminateProcess", BOOL, HANDLE, wintypes.UINT)

PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESTDHANDLES = 0x00000100


def _clamp_size(cols, rows):
    return COORD(max(10, min(500, int(cols))), max(3, min(300, int(rows))))


class ConPty:
    def __init__(self, cmdline: str, cols: int, rows: int, cwd: str, on_output, on_exit):
        self._on_output, self._on_exit = on_output, on_exit
        self.hpc = LPVOID()
        in_r, in_w, out_r, out_w = HANDLE(), HANDLE(), HANDLE(), HANDLE()
        if not CreatePipe(ctypes.byref(in_r), ctypes.byref(in_w), None, 0) or \
                not CreatePipe(ctypes.byref(out_r), ctypes.byref(out_w), None, 0):
            raise OSError(ctypes.get_last_error(), "CreatePipe failed")
        hr = CreatePseudoConsole(_clamp_size(cols, rows), in_r, out_w, 0, ctypes.byref(self.hpc))
        CloseHandle(in_r)   # the pseudo console holds its own duplicates
        CloseHandle(out_w)
        if hr != 0:
            CloseHandle(in_w)
            CloseHandle(out_r)
            raise OSError(f"CreatePseudoConsole failed: 0x{hr & 0xFFFFFFFF:08X}")
        self._in, self._out = in_w, out_r
        size = ctypes.c_size_t()
        InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        self._attr_buf = ctypes.create_string_buffer(size.value)
        attrs = ctypes.cast(self._attr_buf, LPVOID)
        if not InitializeProcThreadAttributeList(attrs, 1, 0, ctypes.byref(size)) or \
                not UpdateProcThreadAttribute(attrs, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                                              self.hpc, ctypes.sizeof(LPVOID), None, None):
            self._close_handles()
            raise OSError(ctypes.get_last_error(), "proc thread attribute setup failed")
        si = STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        # Null std handles + USESTDHANDLES: otherwise a child can bind to the
        # server's own console handles instead of the pseudo console.
        si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        si.lpAttributeList = attrs
        pi = PROCESS_INFORMATION()
        ok = CreateProcessW(None, ctypes.create_unicode_buffer(cmdline), None, None, False,
                            EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT, None,
                            cwd, ctypes.byref(si), ctypes.byref(pi))
        DeleteProcThreadAttributeList(attrs)
        if not ok:
            err = ctypes.get_last_error()
            self._close_handles()
            raise OSError(err, f"could not start {cmdline!r}")
        CloseHandle(pi.hThread)
        self._proc = pi.hProcess
        self.pid = pi.dwProcessId
        self._closed = False             # closed by us: stop reporting output / exit
        self._lock = threading.Lock()    # guards handles against use-after-close
        self._eof = threading.Event()
        threading.Thread(target=self._read_loop, daemon=True, name="pty-read").start()
        threading.Thread(target=self._wait_loop, daemon=True, name="pty-wait").start()

    def _read_loop(self):
        # Drain until EOF, even while closing: ClosePseudoConsole blocks
        # until conhost has flushed its final output into this pipe.
        buf = ctypes.create_string_buffer(65536)
        n = DWORD()
        while ReadFile(self._out, buf, len(buf), ctypes.byref(n), None) and n.value:
            if not self._closed:
                self._on_output(buf.raw[:n.value])
        self._eof.set()

    def _wait_loop(self):
        WaitForSingleObject(self._proc, 0xFFFFFFFF)
        code = DWORD()
        GetExitCodeProcess(self._proc, ctypes.byref(code))
        # The shell's last words (e.g. "no WSL distribution installed") are
        # still inside conhost; closing the console flushes them to us first.
        self._close_console()
        self._eof.wait(2)
        if not self._closed:
            self._on_exit(code.value)
        with self._lock:
            self._closed = True
            for h in (self._in, self._out, self._proc):
                CloseHandle(h)
            self._in = self._out = None

    def _close_console(self):
        with self._lock:
            hpc, self.hpc = self.hpc, LPVOID()
        if hpc:
            ClosePseudoConsole(hpc)

    def write(self, data: bytes):
        if not data:
            return
        with self._lock:
            if self._closed or not self._in:
                return
            n = DWORD()
            WriteFile(self._in, data, len(data), ctypes.byref(n), None)

    def resize(self, cols: int, rows: int):
        with self._lock:
            if not self._closed and self.hpc:
                ResizePseudoConsole(self.hpc, _clamp_size(cols, rows))

    def _close_handles(self):  # constructor error paths only
        for h in ("_in", "_out"):
            if getattr(self, h, None):
                CloseHandle(getattr(self, h))
                setattr(self, h, None)
        if self.hpc:
            ClosePseudoConsole(self.hpc)
            self.hpc = LPVOID()

    def close(self):
        """End the shell; the wait thread releases the handles once it exits."""
        if self._closed:
            return
        self._closed = True

        def _finish():
            self._close_console()  # ends the console session -> the shell exits
            if WaitForSingleObject(self._proc, 2000) != 0:
                TerminateProcess(self._proc, 1)
        threading.Thread(target=_finish, daemon=True, name="pty-close").start()


# --------------------------------------------------------------------------
def available_shells() -> list[dict]:
    sysdir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    shells = [{"id": "ps", "name": "PowerShell", "cmd": "powershell.exe -NoLogo"}]
    pwsh = shutil.which("pwsh")
    if pwsh:
        shells.append({"id": "pwsh", "name": "PowerShell 7", "cmd": f'"{pwsh}" -NoLogo'})
    shells.append({"id": "cmd", "name": "Command Prompt", "cmd": "cmd.exe"})
    if os.path.exists(os.path.join(sysdir, "wsl.exe")):
        shells.append({"id": "wsl", "name": "WSL", "cmd": "wsl.exe --cd ~"})
    return shells


REPLAY_BYTES = 512 * 1024
IDLE_REAP_S = 30 * 60


class TermSession:
    def __init__(self, sid: str, shell: dict, cols: int, rows: int):
        self.sid, self.shell = sid, shell
        self.cols, self.rows = cols, rows
        self._lock = threading.Lock()
        self._clients: set = set()       # wsock.Outbox per attached client
        self._buf = bytearray()
        self.detached_at = time.monotonic()
        self.exited = None
        self.pty = ConPty(shell["cmd"], cols, rows, os.path.expanduser("~"), self._output, self._exit)

    def _output(self, data: bytes):
        with self._lock:
            self._buf += data
            if len(self._buf) > REPLAY_BYTES:
                # Trim at a line boundary so replay doesn't start mid-sequence.
                cut = self._buf.find(b"\n", len(self._buf) - REPLAY_BYTES)
                del self._buf[:cut + 1 if cut >= 0 else len(self._buf) - REPLAY_BYTES]
            for out in self._clients:
                out.send(data)  # queued: a stalled phone can't block the shell

    def _exit(self, code):
        with self._lock:
            self.exited = code
            for out in self._clients:
                out.send_json({"t": "exit", "code": code})

    def attach(self, out):
        # Under the lock: output produced meanwhile is queued *after* the
        # replay, never lost to the client's reset or drawn out of order.
        with self._lock:
            out.send_json({"t": "ready", "sid": self.sid, "shell": self.shell["id"],
                           "name": self.shell["name"], "replay": bool(self._buf)})
            if self._buf:
                out.send(bytes(self._buf))
            if self.exited is not None:
                out.send_json({"t": "exit", "code": self.exited})
            self._clients.add(out)

    def detach(self, out):
        with self._lock:
            self._clients.discard(out)
            if not self._clients:
                self.detached_at = time.monotonic()

    @property
    def idle(self):
        return not self._clients and time.monotonic() - self.detached_at > IDLE_REAP_S

    def close(self):
        self.pty.close()
        with self._lock:
            if self.exited is None:
                self.exited = -1
                for out in self._clients:  # others still attached learn it ended
                    out.send_json({"t": "exit", "code": -1})


class _Registry:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict = {}
        threading.Thread(target=self._reap, daemon=True, name="term-reaper").start()

    def get_or_create(self, sid: str, shell_id: str, cols: int, rows: int, fresh=False) -> TermSession:
        shells = {s["id"]: s for s in available_shells()}
        shell = shells.get(shell_id) or shells["ps"]
        with self._lock:
            sess = self._sessions.get(sid) if sid else None
            if sess and (fresh or sess.exited is not None or sess.shell["id"] != shell["id"]):
                sess.close()
                self._sessions.pop(sess.sid, None)
                sess = None
            if not sess:
                sid = sid or secrets.token_urlsafe(9)
                sess = TermSession(sid, shell, cols, rows)
                self._sessions[sid] = sess
            return sess

    def drop(self, sess: TermSession):
        with self._lock:
            if self._sessions.get(sess.sid) is sess:
                del self._sessions[sess.sid]
        sess.close()

    def _reap(self):
        while True:
            time.sleep(60)
            with self._lock:
                dead = [s for s in self._sessions.values() if s.idle]
                for s in dead:
                    self._sessions.pop(s.sid, None)
            for s in dead:
                s.close()


REGISTRY = _Registry()


def serve(ws, query: dict):
    def q(name, default):
        return query.get(name, [default])[0]
    cols, rows = int(q("cols", "100")), int(q("rows", "30"))
    out = Outbox(ws, 8 << 20)
    try:
        sess = REGISTRY.get_or_create(q("sid", ""), q("shell", "ps"), cols, rows)
    except OSError as exc:
        out.send_json({"t": "error", "msg": str(exc)})
        out.close()
        return
    sess.attach(out)
    if (cols, rows) != (sess.cols, sess.rows):
        sess.cols, sess.rows = cols, rows
        sess.pty.resize(cols, rows)
    try:
        for msg in ws.recv_json():
            if isinstance(msg, bytes):
                sess.pty.write(msg)
                continue
            t = msg.get("t")
            if t == "resize":
                size = int(msg.get("cols", cols)), int(msg.get("rows", rows))
                if size != (sess.cols, sess.rows):  # a no-op resize still makes ConPTY repaint
                    sess.cols, sess.rows = size
                    sess.pty.resize(*size)
            elif t == "input":
                sess.pty.write(str(msg.get("data", "")).encode("utf-8"))
            elif t == "kill":
                sess.detach(out)
                REGISTRY.drop(sess)  # a later connect with this sid starts a new shell
                out.send_json({"t": "exit", "code": -1})
            elif t == "restart":
                sess.detach(out)
                try:
                    sess = REGISTRY.get_or_create(sess.sid, msg.get("shell", sess.shell["id"]),
                                                  sess.cols, sess.rows, fresh=True)
                except OSError as exc:
                    out.send_json({"t": "error", "msg": str(exc)})
                    continue
                sess.attach(out)
    finally:
        sess.detach(out)
        out.close()
