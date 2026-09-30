"""Minimal, thread-safe RFC 6455 WebSocket (server side) on top of
BaseHTTPRequestHandler. Handles fragmentation, ping/pong and close; one
reader thread + any number of writer threads per socket."""
import base64
import hashlib
import json
import socket
import struct
import threading
from collections import deque

_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


def _unmask(payload: bytes, mask: bytes) -> bytes:
    # XOR via big-int arithmetic: O(n) in C instead of a Python byte loop.
    n = len(payload)
    if not n:
        return b""
    key = int.from_bytes((mask * ((n + 3) // 4))[:n], "little")
    return (int.from_bytes(payload, "little") ^ key).to_bytes(n, "little")


class WebSocket:
    def __init__(self, handler):
        self.sock: socket.socket = handler.connection
        self.rfile = handler.rfile
        self._wlock = threading.Lock()
        self.closed = False

    @classmethod
    def accept(cls, handler, sndbuf: int = 0) -> "WebSocket | None":
        """Perform the upgrade handshake. Returns None (after replying 400)
        if the request is not a valid WebSocket upgrade."""
        key = handler.headers.get("Sec-WebSocket-Key", "")
        if not key or "websocket" not in handler.headers.get("Upgrade", "").lower():
            handler.send_error(400, "expected websocket upgrade")
            return None
        accept = base64.b64encode(hashlib.sha1((key + _MAGIC).encode()).digest()).decode()
        handler.send_response(101)
        handler.send_header("Upgrade", "websocket")
        handler.send_header("Connection", "Upgrade")
        handler.send_header("Sec-WebSocket-Accept", accept)
        handler.end_headers()
        handler.close_connection = True
        ws = cls(handler)
        try:
            # Nagle would hold the tail of every frame waiting for an ACK.
            ws.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            ws.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if sndbuf:
                # A bounded kernel buffer keeps congestion visible to us
                # (instead of hiding seconds of video in the OS queue).
                ws.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, sndbuf)
        except OSError:
            pass
        return ws

    # --- reading ---------------------------------------------------------
    def _read_exact(self, n: int) -> bytes:
        data = self.rfile.read(n)
        if data is None or len(data) < n:
            raise ConnectionError("websocket closed")
        return data

    def recv(self):
        """Return (opcode, payload) for the next text/binary message, or
        (None, None) once the peer closes / disconnects."""
        parts, first_op = [], None
        try:
            while True:
                b0, b1 = self._read_exact(2)
                fin, op = b0 & 0x80, b0 & 0x0F
                n = b1 & 0x7F
                if n == 126:
                    n = struct.unpack("!H", self._read_exact(2))[0]
                elif n == 127:
                    n = struct.unpack("!Q", self._read_exact(8))[0]
                mask = self._read_exact(4) if b1 & 0x80 else b""
                payload = self._read_exact(n) if n else b""
                if mask:
                    payload = _unmask(payload, mask)
                if op == OP_CLOSE:
                    self.close()
                    return None, None
                if op == OP_PING:
                    self._send_frame(OP_PONG, payload)
                    continue
                if op == OP_PONG:
                    continue
                if op != OP_CONT:
                    first_op, parts = op, []
                parts.append(payload)
                if fin:
                    return first_op, b"".join(parts)
        except (OSError, ValueError, ConnectionError, struct.error):
            self.closed = True
            return None, None

    def recv_json(self):
        """Yield decoded JSON text messages until the socket closes; binary
        messages are yielded as raw bytes."""
        while True:
            op, data = self.recv()
            if op is None:
                return
            if op == OP_TEXT:
                try:
                    yield json.loads(data)
                except ValueError:
                    continue
            else:
                yield data

    # --- writing ---------------------------------------------------------
    def _send_frame(self, op: int, payload: bytes):
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", 0x80 | op, n)
        elif n < 65536:
            header = struct.pack("!BBH", 0x80 | op, 126, n)
        else:
            header = struct.pack("!BBQ", 0x80 | op, 127, n)
        with self._wlock:
            if self.closed and op != OP_CLOSE:
                raise ConnectionError("websocket closed")
            self.sock.sendall(header + payload if n < 65536 else header)
            if n >= 65536:
                self.sock.sendall(payload)

    def send(self, data) -> bool:
        """Send str (text) or bytes (binary). Returns False if the socket is
        gone, so callers can simply stop."""
        try:
            if isinstance(data, str):
                self._send_frame(OP_TEXT, data.encode("utf-8"))
            else:
                self._send_frame(OP_BIN, data)
            return True
        except (OSError, ConnectionError, ValueError):
            self.closed = True
            return False

    def send_json(self, obj) -> bool:
        return self.send(json.dumps(obj, separators=(",", ":")))

    def close(self, code: int = 1000):
        if self.closed:
            return
        try:
            self._send_frame(OP_CLOSE, struct.pack("!H", code))
        except (OSError, ConnectionError, ValueError):
            pass
        self.abort()

    def abort(self):
        """Drop the connection now (no close handshake). Unblocks any thread
        stuck in sendall/recv on this socket."""
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class Outbox:
    """Queued, non-blocking sends for fan-out. The thread that feeds many
    clients (cursor watcher, a shell's output) must never block on one
    client whose Wi-Fi vanished; a client that falls too far behind is
    disconnected instead (it reconnects and resyncs)."""

    def __init__(self, ws: WebSocket, limit: int = 4 << 20):
        self.ws, self.limit = ws, limit
        self._q = deque()
        self._bytes = 0
        self._cv = threading.Condition()
        self._closed = False
        threading.Thread(target=self._run, daemon=True, name="ws-outbox").start()

    def send(self, data) -> bool:
        with self._cv:
            if self._closed:
                return False
            self._q.append(data)
            self._bytes += len(data)
            if self._bytes > self.limit:
                self._closed = True
                self._q.clear()
                self._cv.notify()
                self.ws.abort()
                return False
            self._cv.notify()
        return True

    def send_json(self, obj) -> bool:
        return self.send(json.dumps(obj, separators=(",", ":")))

    def _run(self):
        while True:
            with self._cv:
                while not self._q and not self._closed:
                    self._cv.wait()
                if not self._q:
                    return
                item = self._q.popleft()
                self._bytes -= len(item)
            if not self.ws.send(item):
                with self._cv:
                    self._closed = True
                    self._q.clear()
                return

    def close(self):
        with self._cv:
            self._closed = True
            self._cv.notify()
