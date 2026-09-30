"""Tiled JPEG: send only what changed on screen.

A Watcher (one per display, shared by every viewer) captures the desktop
with DXGI desktop duplication and keeps, for each picture size a viewer
wants (a View), its own copy of the screen at that size, shrunk on the GPU.
Windows reports which areas changed; those are only a hint of where to
look: each 32x32 tile there is compared with the previous frame, so what
counts as changed is exact. (A full comparison once a second catches
anything the hints missed.) Each tile remembers the frame number of its
last change.

A TileEncoder (one per viewer stream) sends whatever changed since its last
send, as a few merged rectangles, each one JPEG, drawn by the phone over
its copy of the screen. Changes pile up while the link is busy and go out
together, from the newest capture, so nothing is lost and nothing queues.
When most of the screen changed it sends the full screen, as a few
horizontal bands (browsers decode separate images in parallel). Once an
area has been still for a moment it's resent at a higher quality
("refinement"): moving content stays light, still content ends up sharp.

The encoder duck-types video.Encoder for StreamSession; instead of pushing
finished frames it signals "new content" and the session's sender calls
take() when the link has room, so every send uses the newest pixels."""
import ctypes
import math
import os
import struct
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

try:
    import numpy as np
    import simplejpeg
    import dxgicap
except ImportError:  # optional dependencies: the plain (ffmpeg) JPEG stream still works
    np = None

TILE = 32           # pixels per change-tracking tile (a multiple of the 16px JPEG macroblock)
MAX_RECTS = 12      # more separate areas than this get merged
FULL_SHARE = 0.5    # changed area above this share of the screen -> one full-screen JPEG
STILL_S = 0.3       # an area still for this long gets refined
REFINE_SHARE = 0.25 # screen share refined per message when nothing else changes
REFINE_SIDE = 0.04  # ... when it travels with live changes (mustn't slow them down)
FULL_CHECK_S = 1.0  # full comparison this often, in case the hints miss something
FULL_BANDS = 4      # a full screen goes out as this many horizontal JPEGs: browsers decode
                    # separate images in parallel (~2.5x faster in Chrome), for ~3% more bytes
PARALLEL_SHARE = 0.15  # messages covering more of the screen encode their JPEGs in parallel

_pool = None  # simplejpeg releases the GIL, so threads encode truly in parallel


def _encoder_pool():
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=min(FULL_BANDS, os.cpu_count() or 2), thread_name_prefix="jpeg")
    return _pool

# ffmpeg MJPEG q:v -> libjpeg quality giving the same size (measured on a desktop screenshot)
_QV = {1: 92, 2: 88, 3: 82, 4: 76, 5: 70, 6: 63, 7: 57, 8: 50, 9: 47, 10: 43, 11: 40, 12: 37, 13: 34, 14: 31}


def available() -> bool:
    return np is not None and os.environ.get("PC_JPEG_TILES", "1") != "0"


def quality(qv: int) -> int:
    return _QV[min(14, max(1, int(qv)))]


def merge_rects(mask, limit=MAX_RECTS):
    """Tile mask -> a few rectangles [tx0, ty0, tx1, ty1] covering it: runs
    per row, stacked while identical, then the cheapest pairs merged."""
    rects, open_ = [], {}
    for y in range(mask.shape[0]):
        row = mask[y]
        if not row.any():
            rects += open_.values()
            open_ = {}
            continue
        d = np.diff(np.concatenate(([0], row.view(np.int8), [0])))
        nxt = {}
        for x0, x1 in zip(np.flatnonzero(d == 1).tolist(), np.flatnonzero(d == -1).tolist()):
            r = open_.pop((x0, x1), None)
            if r:
                r[3] = y + 1
            else:
                r = [x0, y, x1, y + 1]
            nxt[(x0, x1)] = r
        rects += open_.values()
        open_ = nxt
    rects += open_.values()
    if len(rects) > 4 * limit:  # scattered: one bounding box
        return [[min(r[0] for r in rects), min(r[1] for r in rects),
                 max(r[2] for r in rects), max(r[3] for r in rects)]]
    while len(rects) > limit:
        best = None
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                a, b = rects[i], rects[j]
                u = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                cost = _area(u) - _area(a) - _area(b)
                if best is None or cost < best[0]:
                    best = (cost, i, j, u)
        _, i, j, u = best
        rects[i] = u
        rects.pop(j)
    return rects


def _area(r):
    return (r[2] - r[0]) * (r[3] - r[1])


# --------------------------------------------------------------------------
# Screen watcher (one per display) and its views (one per picture size)
# --------------------------------------------------------------------------
class View:
    """The screen at w x h, kept current by the watcher."""

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.th, self.tw = -(-h // TILE), -(-w // TILE)
        self.frame = np.zeros((h, w, 4), np.uint8)   # BGRX
        self.frame32 = self.frame.view(np.uint32).reshape(h, w)
        self.ver = np.zeros((self.th, self.tw), np.uint32)     # frame number of each tile's last change
        self.changed_at = np.zeros((self.th, self.tw), np.float64)
        self.count = 0              # frames with changes so far (0 = no image yet)
        self.captured_at = 0.0      # perf_counter ms of the newest image
        self.lock = threading.Lock()
        self.subs: set = set()

    def regions(self, hints, W, H, full):
        """Native change hints -> areas of this view to re-read (its pixels,
        tile-aligned); None = everything, [] = nothing."""
        if full or hints is dxgicap.Duplication.ALL or not self.count:
            return None
        rx, ry = W / self.w, H / self.h
        mask = np.zeros((self.th, self.tw), bool)
        for x0, y0, x1, y1 in hints:
            # every output pixel whose footprint touches the changed source pixels
            a, b = max(0, math.floor(x0 / rx) - 1), min(self.w, math.ceil(x1 / rx) + 1)
            c, d = max(0, math.floor(y0 / ry) - 1), min(self.h, math.ceil(y1 / ry) + 1)
            if a < b and c < d:
                mask[c // TILE:-(-d // TILE), a // TILE:-(-b // TILE)] = True
        if not mask.any():
            return []
        return [(x0 * TILE, y0 * TILE, min(self.w, x1 * TILE), min(self.h, y1 * TILE))
                for x0, y0, x1, y1 in merge_rects(mask, 32)]

    def absorb(self, addr, pitch, regions):
        """Compares the GPU copy with ours in `regions`, takes the changes,
        and bumps the changed tiles' version. -> subscribers to notify."""
        src = np.frombuffer((ctypes.c_uint8 * (pitch * self.h)).from_address(addr), np.uint8)
        src32 = src.reshape(self.h, pitch)[:, :self.w * 4].view(np.uint32)
        changed = np.zeros((self.th, self.tw), bool)
        for x0, y0, x1, y1 in regions or [(0, 0, self.w, self.h)]:
            a, b = src32[y0:y1, x0:x1], self.frame32[y0:y1, x0:x1]
            ne = a != b
            if not ne.any():
                continue
            per_tile = np.add.reduceat(np.add.reduceat(ne, np.arange(0, y1 - y0, TILE), axis=0),
                                       np.arange(0, x1 - x0, TILE), axis=1) > 0
            changed[y0 // TILE:-(-y1 // TILE), x0 // TILE:-(-x1 // TILE)] |= per_tile
            b[...] = a  # pixels first, then the version bump that announces them
        if not changed.any() and self.count:
            return []
        now = time.perf_counter() * 1000
        with self.lock:
            first = not self.count
            self.count += 1
            if first:
                self.ver[:] = self.count
                self.changed_at[:] = now
            else:
                self.ver[changed] = self.count
                self.changed_at[changed] = now
            self.captured_at = now
            return list(self.subs)


class Watcher:
    _all: dict = {}
    _all_lock = threading.Lock()

    @classmethod
    def get(cls, display) -> "Watcher":
        key = (display["adapter"], display["output"])
        with cls._all_lock:
            w = cls._all.get(key)
            if w is None or w.dead:
                w = cls._all[key] = Watcher(display)
            return w

    def __init__(self, display):
        self.adapter, self.output = display["adapter"], display["output"]
        self.W, self.H = display["w"], display["h"]
        self.lock = threading.Lock()
        self.views: dict = {}
        self.dead = False
        self._thread = None

    def subscribe(self, sub, w, h) -> View | None:
        """None if this watcher has just shut down (take a new one)."""
        with self.lock:
            if self.dead:
                return None
            v = self.views.get((w, h))
            if v is None:
                v = self.views[(w, h)] = View(w, h)
            v.subs.add(sub)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True, name="tiles-capture")
                self._thread.start()
        if v.count:
            sub._changed()
        return v

    def unsubscribe(self, sub, view):
        with self.lock:
            if view:
                view.subs.discard(sub)

    def _active(self):
        """-> (views with viewers, frame rate they want); drops unused views."""
        with self.lock:
            for k in [k for k, v in self.views.items() if not v.subs]:
                del self.views[k]
            views = list(self.views.values())
            return views, max((s.spec.fps for v in views for s in v.subs), default=0)

    def _run(self):
        try:
            self._loop()
        except Exception as e:  # noqa: BLE001 - anything unexpected: viewers fall back to ffmpeg's JPEG
            import logging
            logging.getLogger("pc-remote").exception("tiled JPEG capture failed")
            self._fail(f"capture failed: {e}")

    def _loop(self):
        dup, fails, last_full, next_due, last_tick, sizes = None, 0, 0.0, 0.0, 0.0, set()
        try:
            while True:
                views, fps = self._active()
                if not views:
                    with Watcher._all_lock, self.lock:
                        if not any(v.subs for v in self.views.values()):  # last viewer left
                            self.dead = True
                            Watcher._all.pop((self.adapter, self.output), None)
                            return
                    continue
                if dup is None:
                    try:
                        dup = dxgicap.Duplication(self.adapter, self.output)
                        if (dup.width, dup.height) != (self.W, self.H) or dup.rotation not in (0, 1):
                            raise dxgicap.CaptureError("display size or rotation changed")
                        fails, sizes = 0, set()
                    except dxgicap.CaptureError as e:
                        if dup:
                            dup.close()
                        dup = None
                        fails += 1
                        # Never worked: give up (the stream falls back to ffmpeg).
                        # Worked before: probably a UAC prompt / lock screen; wait it out.
                        if (not any(v.count for v in views) and fails > 15) or "size" in str(e):
                            self._fail(str(e))
                            return
                        time.sleep(0.2)
                        continue
                for k in sizes - {(v.w, v.h) for v in views}:
                    dup.drop(*k)  # GPU copies nobody uses any more
                sizes = {(v.w, v.h) for v in views}
                now = time.perf_counter()
                if now - last_tick > 0.1:
                    last_tick = now
                    for v in views:  # refinement checks, also while something keeps animating
                        for s in list(v.subs):
                            s._tick()
                wait = next_due - now
                if wait > 0:
                    time.sleep(wait)  # changes accumulate in DXGI meanwhile
                try:
                    hints = dup.acquire(100)
                    if hints is None:  # nothing new
                        continue
                    if hints == []:
                        dup.release()
                        continue
                    full = time.perf_counter() - last_full > FULL_CHECK_S
                    work = []
                    for v in views:
                        regions = v.regions(hints, self.W, self.H, full)
                        if regions == []:
                            continue
                        t = dup.target(v.w, v.h)
                        dup.render(t)
                        dup.copy(t, None if regions is None or len(regions) > 16 else regions)
                        work.append((v, t, regions))
                    dup.release()
                    notify = []
                    for v, t, regions in work:
                        addr, pitch = dup.map(t)
                        try:
                            notify += v.absorb(addr, pitch, regions)
                        finally:
                            dup.unmap(t)
                except dxgicap.CaptureError as e:
                    dup.close()
                    dup = None
                    if not e.lost:
                        time.sleep(0.2)
                    continue
                if full:
                    last_full = time.perf_counter()
                next_due = time.perf_counter() + 1 / fps
                for s in notify:
                    s._changed()
        finally:
            if dup:
                dup.close()

    def _fail(self, err):
        with Watcher._all_lock, self.lock:
            self.dead = True
            Watcher._all.pop((self.adapter, self.output), None)
            subs = [s for v in self.views.values() for s in v.subs]
        for s in subs:
            s._failed(err)


# --------------------------------------------------------------------------
# Per-viewer encoder
# --------------------------------------------------------------------------
_RECT = struct.Struct("<HHHHI")


class TileEncoder:
    """One viewer's tiled stream at spec.w x spec.h. Message payload:
    u16 count, count x (u16 x, y, w, h, u32 jpeg length), then the JPEGs."""

    def __init__(self, spec, gen, on_packet, on_exit):
        self.spec, self.gen = spec, gen
        self._on_packet, self._on_exit = on_packet, on_exit
        self.started = time.monotonic()
        self.first_frame = None
        self.frames = self.bytes = 0
        self.errors: deque = deque(maxlen=12)
        self.reason = ""
        self._stopped = False
        self._watch = self._view = None
        self._taken = 0          # view frame number already sent
        self._full = True        # next send is the whole screen
        self._lock = threading.Lock()
        self._sent_q = None      # quality each tile was last sent at
        self._refine_posted = False
        self.retune(spec)

    # ---- Encoder interface --------------------------------------------------
    def start(self):
        th, tw = -(-self.spec.h // TILE), -(-self.spec.w // TILE)
        self._sent_q = np.zeros((th, tw), np.int16)
        while self._view is None:  # a watcher whose last viewer just left may be shutting down
            self._watch = Watcher.get(self.spec.display)
            self._view = self._watch.subscribe(self, self.spec.w, self.spec.h)
        return self

    def stop(self):
        self._stopped = True
        if self._watch:
            self._watch.unsubscribe(self, self._view)

    @property
    def alive(self):
        return not self._stopped and self._watch is not None and not self._watch.dead

    def retune(self, spec):
        """Quality or frame rate changed (same size): applies from the next send."""
        self.spec = spec
        self._q = quality(spec.t["jpeg"])
        self._q_still = max(self._q, quality(spec.t.get("jpeg_still", spec.t["jpeg"])))

    def refresh(self):
        self._full = True
        self._post(False)

    # ---- watcher callbacks --------------------------------------------------
    def _changed(self):
        self._post(True)

    def _tick(self):
        if not self._refine_posted and self._view and self._view.count \
                and self._refine_mask(time.perf_counter() * 1000).any():
            self._post(False)

    def _failed(self, err):
        self.errors.append(err)
        if not self._stopped:
            self._on_exit(self)

    def _post(self, change):
        if self._stopped:
            return
        if not change:
            self._refine_posted = True
        self._on_packet(self, "tiles", "change" if change else "refine", self._full)

    # ---- building a message (the session's sender thread) ------------------
    def _refine_mask(self, now):
        v = self._view
        return (self._sent_q < self._q_still) & (now - v.changed_at > STILL_S * 1000) & (v.ver <= self._taken)

    def take(self):
        """-> (payload, capture time ms, full screen?, more to refine?) or None."""
        v = self._view
        if v is None or self._stopped:
            return None
        with self._lock:
            now = time.perf_counter() * 1000
            with v.lock:
                count, t_cap = v.count, v.captured_at
                if not count:
                    return None
                full = self._full
                dirty = None if full else v.ver > self._taken
                self._taken, self._full = count, False
            self._refine_posted = False
            bands = [([0, v.th * i // FULL_BANDS, v.tw, v.th * (i + 1) // FULL_BANDS], self._q)
                     for i in range(FULL_BANDS) if v.th * i // FULL_BANDS < v.th * (i + 1) // FULL_BANDS]
            if full:
                jobs = bands
            else:
                jobs = [(r, self._q) for r in merge_rects(dirty)] if dirty.any() else []
                if sum(_area(r) for r, _ in jobs) > FULL_SHARE * v.tw * v.th:
                    full, jobs = True, bands
            if not full:
                # Still areas get their sharper copy, a band at a time: a small one
                # next to live changes, a bigger one when nothing else is going on.
                # Riding along with changes means a video playing in a corner can't
                # starve the rest of the screen.
                ref = self._refine_mask(now)
                if ref.any():
                    budget = int((REFINE_SIDE if jobs else REFINE_SHARE) * v.tw * v.th)
                    for x0, y0, x1, y1 in sorted(merge_rects(ref), key=lambda r: (r[1], r[0])):
                        if budget <= 0:
                            break
                        rows = min(y1 - y0, max(1, budget // (x1 - x0)))
                        jobs.append(([x0, y0, x1, y0 + rows], self._q_still))
                        budget -= rows * (x1 - x0)
            if not jobs:
                return None
            rects = [((tx0 * TILE, ty0 * TILE, min(v.w, tx1 * TILE), min(v.h, ty1 * TILE)), q)
                     for (tx0, ty0, tx1, ty1), q in jobs]

            def encode(job):
                (x0, y0, x1, y1), q = job
                return simplejpeg.encode_jpeg(np.ascontiguousarray(v.frame[y0:y1, x0:x1]), quality=q,
                                              colorspace="BGRX", colorsubsampling="420", fastdct=True)
            big = len(rects) > 1 and sum(_area(r) for r, _ in rects) > PARALLEL_SHARE * v.w * v.h
            blobs = list(_encoder_pool().map(encode, rects)) if big else [encode(j) for j in rects]
            head = [struct.pack("<H", len(rects))]
            for ((x0, y0, x1, y1), _), jpg in zip(rects, blobs):
                head.append(_RECT.pack(x0, y0, x1 - x0, y1 - y0, len(jpg)))
            for (tx0, ty0, tx1, ty1), q in jobs:
                self._sent_q[ty0:ty1, tx0:tx1] = q
            payload = b"".join(head + blobs)
            self.frames += 1
            self.bytes += len(payload)
            if self.first_frame is None:
                self.first_frame = time.monotonic()
            return payload, t_cap, full, bool(self._refine_mask(now).any())
