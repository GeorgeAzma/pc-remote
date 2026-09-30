"""Remote input over WebSocket, plus a shared watcher that pushes the PC's
cursor position/shape and clipboard changes to every connected client.

The cursor is NOT burned into the video: the client draws it as an overlay
and predicts its motion locally from the deltas it sends, so the pointer
tracks the finger with zero perceived latency. The server's authoritative
position (echoed with the last applied input seq) reconciles the
prediction, and also covers the physical mouse being moved at the PC.

Client -> server (JSON):
  {t:'mv', dx, dy, s}     relative move in screen px (already accelerated)
  {t:'abs', x, y, s}      absolute move, virtual-desktop px
  {t:'btn', b, d}         button down/up ('left'|'right'|'middle'|'x1'|'x2')
  {t:'click', b, n}       click n times
  {t:'wheel', dy, dx}     wheel units (120 = one notch)
  {t:'key', code, d}      physical key (KeyboardEvent.code) down/up
  {t:'text', s}           type Unicode text
  {t:'bs', n}             n backspaces (for diff-based soft-keyboard typing)
  {t:'combo', s}          'ctrl+shift+esc'
  {t:'clip', text}        set the PC clipboard
  {t:'rel'}               release every held key/button
Server -> client:
  {t:'hi', displays, host, vs}     on connect
  {t:'cur', x, y, v, id, s}        cursor moved (s = last applied seq)
  {t:'shape', id, w, h, hx, hy, png}
  {t:'clip', text}                 PC clipboard changed
"""
import base64
import socket
import threading
import time

import win32
from wsock import Outbox


class _Hub:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: set = set()
        self._thread = None
        self._shapes: dict = {}     # hcursor -> shape message
        self._state = None
        self._clip_seq = None

    def add(self, sess):
        with self._lock:
            self._sessions.add(sess)
            if not self._thread or not self._thread.is_alive():
                self._clip_seq = win32.clipboard_seq()
                self._thread = threading.Thread(target=self._watch, daemon=True, name="cursor-watch")
                self._thread.start()
        vis, x, y, hc = win32.cursor_state()
        shape = self.shape(hc)
        if shape:
            sess.out.send_json(shape)
        sess.out.send_json({"t": "cur", "x": x, "y": y, "v": vis, "id": hc, "s": 0})

    def remove(self, sess):
        with self._lock:
            self._sessions.discard(sess)

    def shape(self, hc):
        if not hc:
            return None
        if hc not in self._shapes:
            img = None
            try:
                img = win32.cursor_image(hc)
            except OSError:
                pass
            if not img:
                return None
            w, h, hx, hy, rgba = img
            self._shapes[hc] = {"t": "shape", "id": hc, "w": w, "h": h, "hx": hx, "hy": hy,
                                "png": base64.b64encode(win32.png_encode(w, h, rgba)).decode()}
        return self._shapes[hc]

    def broadcast(self, msg, skip=None):
        with self._lock:
            targets = [s for s in self._sessions if s is not skip]
        for s in targets:
            s.out.send_json(msg)  # queued: one vanished phone can't stall the others

    def _watch(self):
        next_clip = 0.0
        while True:
            with self._lock:
                if not self._sessions:
                    self._thread = None
                    return
            state = win32.cursor_state()
            if state != self._state:
                vis, x, y, hc = state
                if not self._state or hc != self._state[3]:
                    shape = self.shape(hc)
                    if shape:
                        self.broadcast(shape)
                self._state = state
                self.broadcast({"t": "cur", "x": x, "y": y, "v": vis, "id": hc})
            now = time.monotonic()
            if now >= next_clip:
                next_clip = now + 0.3
                seq = win32.clipboard_seq()
                if seq != self._clip_seq:
                    self._clip_seq = seq
                    try:
                        text = win32.get_clipboard()
                    except OSError:
                        text = None
                    if text:
                        self.broadcast({"t": "clip", "text": text[:200_000]})
            time.sleep(0.008)  # ~125 Hz


HUB = _Hub()


class InputSession:
    def __init__(self, ws):
        self.ws = ws
        self.out = Outbox(ws, 1 << 20)
        self._held_keys: set = set()
        self._held_buttons: set = set()

    def run(self):
        vs = win32.virtual_screen()
        self.out.send_json({"t": "hi", "displays": win32.displays(), "host": socket.gethostname(),
                           "vs": {"x": vs[0], "y": vs[1], "w": vs[2], "h": vs[3]}})
        HUB.add(self)
        try:
            for m in self.ws.recv_json():
                if isinstance(m, dict):
                    try:
                        self._handle(m)
                    except (ValueError, TypeError, KeyError, OSError) as exc:
                        self.out.send_json({"t": "err", "msg": str(exc)})
        finally:
            HUB.remove(self)
            self.release_all()
            self.out.close()

    def _reply_cursor(self, pos, seq):
        vis, _, _, hc = win32.cursor_state()
        self.out.send_json({"t": "cur", "x": pos[0], "y": pos[1], "v": vis, "id": hc, "s": seq})

    def _handle(self, m):
        t = m.get("t")
        if t == "mv":
            pos = win32.MOUSE.move_by(float(m.get("dx", 0)), float(m.get("dy", 0)))
            self._reply_cursor(pos, m.get("s", 0))
        elif t == "abs":
            pos = win32.MOUSE.move_to(float(m["x"]), float(m["y"]))
            self._reply_cursor(pos, m.get("s", 0))
        elif t == "btn":
            b, down = m.get("b", "left"), bool(m.get("d"))
            win32.MOUSE.button(b, down)
            (self._held_buttons.add if down else self._held_buttons.discard)(b)
        elif t == "click":
            win32.MOUSE.click(m.get("b", "left"), int(m.get("n", 1)))
        elif t == "wheel":
            win32.MOUSE.wheel(float(m.get("dy", 0)), float(m.get("dx", 0)))
        elif t == "key":
            vk = int(m["vk"]) if "vk" in m else win32.VK_CODES.get(m.get("code", ""))
            if not vk:
                return
            down = bool(m.get("d"))
            ext = True if m.get("code") == "NumpadEnter" else None
            win32.key(vk, down, ext)
            (self._held_keys.add if down else self._held_keys.discard)(vk)
        elif t == "text":
            win32.type_text(str(m.get("s", "")))
        elif t == "bs":
            for _ in range(max(0, min(500, int(m.get("n", 1))))):
                win32.key(0x08, True)
                win32.key(0x08, False)
        elif t == "combo":
            win32.send_combo(str(m.get("s", "")))
        elif t == "clip":
            win32.set_clipboard(str(m.get("text", "")))
            HUB._clip_seq = win32.clipboard_seq()  # don't echo our own write back
        elif t == "rel":
            self.release_all()

    def release_all(self):
        # A phone that drops off Wi-Fi mid-drag must not leave Ctrl or the
        # left button stuck down on the PC.
        if self._held_keys:
            win32.release_keys(list(self._held_keys))
            self._held_keys.clear()
        for b in list(self._held_buttons):
            win32.MOUSE.button(b, False)
        self._held_buttons.clear()
