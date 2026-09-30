"""Low-latency screen streaming.

Pipeline per viewer:  ddagrab (DXGI desktop duplication, GPU) ->
    [native: D3D11 texture straight into NVENC, zero-copy]
    [scaled: hwdownload -> swscale -> NV12]
  -> encoder -> container on stdout -> WebSocket -> browser.

Stream modes (the client picks one; see MODES):
  h264 / hevc  NVENC ultra-low-latency, no B-frames, infinite GOP, in FLV.
               FLV tags are length-prefixed, so a frame is known complete the
               instant its bytes arrive (raw Annex-B would need the *next*
               frame's start code: one frame of extra latency). Decoded by
               WebCodecs, which browsers only offer on secure pages.
  mse          the same H.264 as fragmented MP4, one fragment per frame, for a
               <video> element (Media Source Extensions). Works on plain HTTP
               at H.264 quality, with a little more latency (the muxer holds
               each frame until the next arrives, plus the media pipeline).
  jpeg         JPEG, for any browser. Normally tiled (tiles.py): only the parts
               of the screen that changed, as a few small JPEGs the phone paints
               over its copy; a full-screen change is sent whole, in 4 bands. Without
               numpy/simplejpeg, or if in-process capture fails, ffmpeg's
               MJPEG (a whole JPEG per frame, newest wins) takes over.

Rate control: each client ACKs every frame. Rising ACK delay or in-flight
backlog means the link is saturated -> lower the encoder's max bitrate;
quiet links with a busy encoder -> probe upward. ffmpeg can't change
bitrate mid-stream, so a change spawns a second encoder and switches over
at its first keyframe while the old one keeps streaming: no visible pause.
"""
import base64
import glob
import io
import os
import shutil
import socket
import struct
import subprocess
import threading
import time
from collections import deque

import paths
import tiles
import win32

CREATE_NO_WINDOW = 0x08000000
ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000

KIND_PROBE, KIND_FRAME, KIND_JPEG, KIND_FMP4, KIND_TILES = 0, 2, 3, 4, 5
JPEG_TX_MS = 20      # JPEG while a frame reaches the phone this fast (the H.264 player buffers ~30-50 ms)
PROBE_BYTES = 32 * 1024
_HDR = struct.Struct("<BBHId")  # kind, flags, generation, seq, server time (ms)

ENCODERS = {"h264": ["h264_nvenc", "h264_qsv", "h264_amf", "libx264"],
            "hevc": ["hevc_nvenc", "hevc_qsv", "hevc_amf"]}
MODES = {"h264": ("h264", "flv"), "hevc": ("hevc", "flv"), "mse": ("h264", "fmp4"), "jpeg": ("mjpeg", "mpjpeg")}


def now_ms() -> float:
    # perf_counter (QueryPerformanceCounter): time.monotonic() on Windows only
    # ticks every ~15.6 ms, which would quantize every RTT and latency we measure.
    return time.perf_counter() * 1000.0


def tuning(q: float) -> dict:
    """The quality <-> latency/fps slider, q = 0 (fastest) .. 1 (sharpest).

    Every level runs at the display's refresh rate: more frames always
    look better, and the bitrate (or JPEG quality) adapts to the link.
    Faster: rendered slightly below the on-screen size, frames capped near
    one frame-time of bitrate (so nothing queues), the quickest encoder
    preset. Sharper: always full resolution, a lower (better)
    constant-quality target, bigger frames allowed (a detailed frame may
    take a few frame-times to arrive), slower presets that compress
    better."""
    q = min(1.0, max(0.0, q))
    return {
        "q": round(q, 2),
        "fps_cap": 240,
        "scale": 0.75 if q < 0.2 else 1.0,
        "native": q >= 0.75,
        "cq": round(27 - 11 * q),
        "vbv": round(1.25 + 6.75 * q * q, 2),
        "preset": ("p1", "p2", "p4", "p5", "p6")[min(4, int(q * 4.999))],
        "bpp": 0.12 + 0.3 * q,
        "jpeg": round(8 - 6 * q),
        "jpeg_still": max(1, round(8 - 6 * q) - 2),  # tiled JPEG: areas that stopped changing
    }


# --------------------------------------------------------------------------
# ffmpeg discovery / capabilities
# --------------------------------------------------------------------------
def find_ffmpeg() -> str | None:
    # the installed app ships its own (an LGPL build, next to the .exe)
    cand = [os.environ.get("PC_FFMPEG"), os.path.join(paths.APP, "ffmpeg", "ffmpeg.exe"), shutil.which("ffmpeg")]
    local = os.environ.get("LOCALAPPDATA", "")
    cand.append(os.path.join(local, "Microsoft", "WinGet", "Links", "ffmpeg.exe"))
    cand += sorted(glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "*FFmpeg*", "*", "bin", "ffmpeg.exe")), reverse=True)
    for root in (os.environ.get("ProgramFiles", r"C:\Program Files"), "C:\\"):
        cand.append(os.path.join(root, "ffmpeg", "bin", "ffmpeg.exe"))
    cand.append(os.path.join(os.environ.get("USERPROFILE", ""), "scoop", "shims", "ffmpeg.exe"))
    return next((c for c in cand if c and os.path.isfile(c)), None)


FFMPEG = find_ffmpeg()
_caps: set | None = None
_bad_encoders: dict = {}  # encoder -> time it failed (skipped for a minute)


def capabilities() -> set:
    """Names of the encoders/filters this ffmpeg build offers (cached)."""
    global _caps
    if _caps is None or (not _caps and FFMPEG):  # retry once ffmpeg shows up
        _caps = set()
        if FFMPEG:
            for what in ("-encoders", "-filters"):
                try:
                    out = subprocess.run([FFMPEG, "-hide_banner", what], capture_output=True, text=True,
                                         timeout=15, creationflags=CREATE_NO_WINDOW).stdout
                    _caps |= {ln.split()[1] for ln in out.splitlines() if len(ln.split()) > 2}
                except (OSError, subprocess.SubprocessError):
                    pass
    return _caps


def encoders(codec: str) -> list[str]:
    caps = capabilities()
    now = time.monotonic()
    return [e for e in ENCODERS.get(codec, []) if e in caps and now - _bad_encoders.get(e, -1e9) > 60]


def h264_encoders() -> list[str]:
    return encoders("h264")


def status() -> dict:
    caps = capabilities()
    return {"ffmpeg": FFMPEG, "capture": "ddagrab" in caps, "h264": encoders("h264")[:1],
            "hevc": encoders("hevc")[:1], "mjpeg": "mjpeg" in caps or tiles.available()}


def hevc_codec_string(c: bytes) -> str:
    """RFC 6381 codec string ('hvc1.1.6.L120.90') from an hvcC record."""
    space, tier, profile = c[1] >> 6, (c[1] >> 5) & 1, c[1] & 0x1F
    compat = int(f"{int.from_bytes(c[2:6], 'big'):032b}"[::-1], 2)  # bit-reversed
    cons = list(c[6:12])
    while cons and not cons[-1]:
        cons.pop()
    return (f"hvc1.{'' if not space else 'ABC'[space - 1]}{profile}.{compat:X}.{'HL'[not tier]}{c[12]}"
            + "".join(f".{b:X}" for b in cons))


# --------------------------------------------------------------------------
# One ffmpeg process
# --------------------------------------------------------------------------
class Spec:
    """What to encode. Equality of key() decides whether a restart is needed."""

    def __init__(self, mode, display, w, h, fps, bitrate=0, encoder="", t=None):
        self.mode, self.display, self.w, self.h = mode, display, w, h
        self.fps, self.bitrate, self.encoder = fps, bitrate, encoder
        self.t = t or tuning(0.5)
        self.codec, self.container = MODES[mode]

    @property
    def native(self):
        return self.w == self.display["w"] and self.h == self.display["h"]

    def key(self):
        t = self.t
        return (self.mode, self.display["adapter"], self.display["output"], self.w, self.h, self.fps,
                self.bitrate, self.encoder, t["cq"], t["vbv"], t["preset"], t["jpeg"])

    def __repr__(self):
        return f"Spec({self.mode} {self.w}x{self.h}@{self.fps} {self.bitrate / 1e6:.1f}Mb {self.encoder} q={self.t['q']})"


def _build_cmd(spec: Spec) -> list[str]:
    d, t, enc = spec.display, spec.t, spec.encoder
    # dup_frames=1 keeps a steady cadence: a static screen costs only ~100B
    # skip-frames, NVENC keeps refining quality on them, and the phone's
    # Wi-Fi radio never drops into power-save (which adds latency spikes).
    # (MJPEG too: with dup_frames=0 a static desktop never yields even a
    # first frame. Identical screens give byte-identical JPEGs, which the
    # session drops before sending.)
    chain = f"ddagrab=output_idx={d['output']}:framerate={spec.fps}:draw_mouse=0:dup_frames=1"
    gpu_input = enc.endswith("_nvenc") and spec.native
    if not gpu_input:
        # One swscale pass does the resize and the BGRA->YUV conversion.
        fmt = "yuv420p" if enc in ("libx264", "mjpeg") else "nv12"
        size = "" if spec.native else f"{spec.w}:{spec.h}:"
        color = "out_range=pc" if spec.codec == "mjpeg" else "out_color_matrix=bt709:out_range=tv"
        chain += f",hwdownload,format=bgra,scale={size}flags=area:{color},format={fmt}"
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
           "-init_hw_device", f"d3d11va=dx:{d['adapter']}", "-filter_hw_device", "dx",
           "-filter_threads", "4", "-filter_complex", chain, "-an",
           "-fps_mode", "vfr" if spec.codec == "mjpeg" else "passthrough"]
    if spec.codec == "mjpeg":
        return cmd + ["-c:v", "mjpeg", "-q:v", str(t["jpeg"]), "-color_range", "pc",
                      "-flush_packets", "1", "-f", "mpjpeg", "pipe:1"]
    br = int(spec.bitrate)
    buf = max(int(br / max(1, spec.fps) * t["vbv"]), 300_000)  # VBV: bounds each frame's size & delay
    rate = ["-b:v", str(br // 2), "-maxrate", str(br), "-bufsize", str(buf)]
    cap = ["-maxrate", str(br), "-bufsize", str(buf)]
    profile = ["-profile:v", "high" if spec.codec == "h264" else "main"]
    common = ["-bf", "0", "-g", "999999", *profile]
    if enc.endswith("_nvenc"):
        # Keep per-frame encode time well under the frame interval.
        px = spec.w * spec.h * spec.fps
        preset = "p1" if px > 600e6 else min(t["preset"], "p3") if px > 300e6 else t["preset"]
        cmd += ["-c:v", enc, "-preset", preset, "-tune", "ull", "-zerolatency", "1", "-delay", "0",
                # Constant quality, capped: static content refines to CQ,
                # motion is limited by maxrate/VBV (-> bounded frame size).
                "-rc", "vbr", "-cq", str(t["cq"]), "-b:v", "0", *cap, "-rc-lookahead", "0",
                "-forced-idr", "1", *common]
    elif enc.endswith("_qsv"):
        cmd += ["-c:v", enc, "-preset", "veryfast", "-low_delay_brc", "1", "-look_ahead", "0",
                "-async_depth", "1", *rate, *common]
    elif enc.endswith("_amf"):
        cmd += ["-c:v", enc, "-usage", "ultralowlatency", "-quality", "speed", "-rc", "vbr_peak",
                *rate, *common]
    else:
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-crf", str(t["cq"] - 1), *cap, *common]
    if not gpu_input:
        cmd += ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                "-color_range", "tv"]
    if spec.container == "fmp4":
        return cmd + ["-flush_packets", "1", "-movflags", "empty_moov+default_base_moof+frag_every_frame",
                      "-f", "mp4", "pipe:1"]
    return cmd + ["-flush_packets", "1", "-flvflags", "no_duration_filesize", "-f", "flv", "pipe:1"]


class Encoder:
    """Runs ffmpeg for one Spec and emits parsed packets:
    on_packet(enc, kind, payload, key) with kind
      'config'  avcC / hvcC record (FLV), or the ftyp+moov init segment (fMP4)
      'frame'   one access unit: length-prefixed NALs (FLV) or moof+mdat (fMP4)
      'jpeg'    one JPEG image."""

    def __init__(self, spec: Spec, gen: int, on_packet, on_exit):
        self.spec, self.gen = spec, gen
        self._on_packet, self._on_exit = on_packet, on_exit
        self.proc = None
        self.started = time.monotonic()
        self.first_frame = None
        self.frames = 0
        self.bytes = 0
        self.errors: deque = deque(maxlen=12)
        self._stopped = False
        self._lock = threading.Lock()  # start/stop exclusion: a stop never misses the process
        self.reason = ""               # why this encoder was started (shown to the client)

    def start(self):
        with self._lock:
            if self._stopped:
                return self
            self.proc = subprocess.Popen(_build_cmd(self.spec), stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                                         creationflags=CREATE_NO_WINDOW | ABOVE_NORMAL_PRIORITY_CLASS)
        threading.Thread(target=self._read_err, daemon=True, name="ffmpeg-err").start()
        threading.Thread(target=self._read, daemon=True, name="ffmpeg-out").start()
        return self

    def stop(self):
        with self._lock:
            self._stopped = True
            proc = self.proc
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    @property
    def alive(self):
        return self.proc is not None and self.proc.poll() is None and not self._stopped

    def _read_err(self):
        for line in self.proc.stderr:
            self.errors.append(line.decode("utf-8", "replace").strip())

    def _emit(self, kind, payload, key=False):
        if kind != "config":
            self.frames += 1
            self.bytes += len(payload)
            if self.first_frame is None:
                self.first_frame = time.monotonic()
        self._on_packet(self, kind, payload, key)

    def _read(self):
        r = io.BufferedReader(self.proc.stdout, 1 << 20)
        try:
            {"mpjpeg": self._read_mpjpeg, "fmp4": self._read_fmp4, "flv": self._read_flv}[self.spec.container](r)
        except (OSError, ValueError, EOFError):
            pass
        finally:
            if not self._stopped:
                self._on_exit(self)

    @staticmethod
    def _exact(r, n):
        b = r.read(n)
        if len(b) < n:
            raise EOFError
        return b

    def _read_flv(self, r):
        if self._exact(r, 13)[:3] != b"FLV":
            raise ValueError("not flv")
        while True:
            h = self._exact(r, 11)
            size = int.from_bytes(h[1:4], "big")
            body = self._exact(r, size)
            self._exact(r, 4)  # PreviousTagSize
            if h[0] != 9 or size < 5:
                continue  # audio / script tags
            b0 = body[0]
            if b0 & 0x80:  # Enhanced FLV: [ex|frametype|pkttype][fourcc]...
                key, pkt = ((b0 >> 4) & 7) == 1, b0 & 0x0F
                if pkt == 0:
                    self._emit("config", body[5:])
                elif pkt == 1:
                    self._emit("frame", body[8:], key)
                elif pkt == 3:
                    self._emit("frame", body[5:], key)
            else:          # legacy AVC: [frametype|codec][avcpkttype][cts x3]
                key, pkt = (b0 >> 4) == 1, body[1]
                if pkt == 0:
                    self._emit("config", body[5:])
                elif pkt == 1:
                    self._emit("frame", body[5:], key)

    def _read_mpjpeg(self, r):
        while True:
            length = None
            while True:  # multipart headers
                line = r.readline()
                if not line:
                    raise EOFError
                line = line.strip()
                if not line:
                    if length is not None:
                        break
                    continue
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
            self._emit("jpeg", self._exact(r, length), True)

    def _read_fmp4(self, r):
        # ftyp+moov = init segment; then one moof+mdat pair per frame.
        init, moof, first = b"", None, True
        while True:
            hdr = self._exact(r, 8)
            size, typ = struct.unpack(">I4s", hdr)
            if size == 1:  # 64-bit box size
                ext = self._exact(r, 8)
                hdr, size = hdr + ext, struct.unpack(">Q", ext)[0]
            box = hdr + self._exact(r, size - len(hdr))
            if typ in (b"ftyp", b"moov"):
                init += box
                if typ == b"moov":
                    self._emit("config", init)
                    init = b""
            elif typ == b"moof":
                moof = box
            elif typ == b"mdat" and moof:
                self._emit("frame", moof + box, first)  # a new encoder starts with an IDR
                moof, first = None, False


# --------------------------------------------------------------------------
# Per-viewer session
# --------------------------------------------------------------------------
MIN_BITRATE, MAX_BITRATE = 1_500_000, 120_000_000


class StreamSession:
    """Drives one viewer: owns its encoder(s), sends frames, reacts to ACKs.

    Client -> server (JSON):
      {t:'cfg', mode, w, h, fps, display, maxbr}   desired stream
      {t:'ack', s}                                 frame seq received
      {t:'kf'}                                     decoder lost sync
      {t:'ping', c}                                clock sync / RTT
    Server -> client:
      JSON {t:'config', gen, codec, avcc, ...}     new H.264 stream
      JSON {t:'stats', ...}                        once a second
      binary _HDR + payload                        frames
    """

    def __init__(self, ws):
        self.ws = ws
        self._cv = threading.Condition()
        self._switch_lock = threading.RLock()
        self._closed = False
        self._want = None            # client's latest cfg dict
        self.enc: Encoder | None = None
        self._next: Encoder | None = None
        self._gen = 0
        self._queue: deque = deque()   # h264: (gen, key, data, t)
        self._latest = None            # jpeg: newest frame wins
        self._last_jpeg = None
        self._configs = {}             # gen -> config JSON
        self._sent_cfg_gen = -1
        self._seq = 0
        self._inflight = {}            # seq -> (t_sent_ms, bytes)
        self._acked = deque()          # (t_ms, bytes) for delivery rate
        self._rtts = deque()           # (t_ms, rtt) for base RTT
        self._qdelay = 0.0             # smoothed queueing delay (ms), for stats
        self._qsamples = deque()       # (t_ms, queueing delay) for congestion decisions
        self._bitrate = 0
        self._max_bitrate = MAX_BITRATE
        self._last_switch = 0.0
        self._congested_since = None
        self._ceiling = None           # (bitrate that congested, when)
        self._ceiling_hold = 30.0      # seconds to stay below it
        self._small_rtts = deque()     # (t_ms, rtt) of tiny frames = propagation delay
        self._bw = deque()             # (t_ms, bps) link-capacity samples from big frames
        self._pre_probe = 0            # last known-good bitrate before a probe
        self._last_reason = ""
        self._prop = deque()           # (t_ms, rtt) of tiny server pings = propagation delay
        # JPEG: pacing + adaptive quality; the "alt" mode (H.264 player) is
        # the fallback when even low-quality JPEG can't fit the link.
        self._use_alt = False
        self._alt_since = 0.0
        self._jpeg_size = 0.0          # JPEG frame size (B) when we fell back
        self._last_probe = 0.0
        self._alt_retry = 20.0         # seconds on H.264 before trying JPEG again (backs off)
        self._jpeg_since = 0.0
        self._j = {"prod": 0, "skip": 0, "lim": 0, "free": 0, "pen": 0, "size": 0.0,
                   "delays": deque(maxlen=240), "delay": 0.0}
        self._sent_log = deque()       # (t_ms, bytes) of recent sends, for pacing
        self._restarts = deque()
        self._flushing = False
        self.error = None

    # ---- lifecycle -------------------------------------------------------
    def run(self):
        global FFMPEG
        if not FFMPEG or not os.path.isfile(FFMPEG):
            FFMPEG = find_ffmpeg()
        if not FFMPEG:
            self.ws.send_json({"t": "error", "msg": "ffmpeg not found — install it (winget install Gyan.FFmpeg) and restart the server"})
            return
        sender = threading.Thread(target=self._send_loop, daemon=True, name="vsend")
        sender.start()
        try:
            for msg in self.ws.recv_json():
                if isinstance(msg, dict):
                    self._on_message(msg)
        finally:
            with self._cv:
                self._closed = True
                self._cv.notify_all()
            with self._switch_lock:  # let an in-flight switch finish, then stop everything
                for e in (self.enc, self._next):
                    if e:
                        e.stop()

    def _on_message(self, m):
        t = m.get("t")
        if t == "ack":
            self._on_ack(int(m.get("s", 0)))
        elif t == "ping":
            self.ws.send_json({"t": "pong", "c": m.get("c"), "s": now_ms()})
        elif t == "spong":
            now = now_ms()
            with self._cv:
                self._prop.append((now, now - float(m.get("s", now))))
                while self._prop and now - self._prop[0][0] > 10_000:
                    self._prop.popleft()
        elif t == "cfg":
            with self._cv:
                old = self._want
                self._want = m
                self._max_bitrate = int(m.get("maxbr") or MAX_BITRATE)
                if old and (old.get("q") != m.get("q") or old.get("mode") != m.get("mode")):
                    self._bitrate = 0  # new quality level: restart from its own bitrate target
                if old and (old.get("q") != m.get("q") or old.get("mode") != m.get("mode")
                            or old.get("alt") != m.get("alt")):
                    self._use_alt = False
                    self._j["pen"] = 0
            self._reconfigure()
        elif t == "kf":
            self._switch(flush=True, reason="keyframe request")

    # ---- spec selection -------------------------------------------------
    def _target_spec(self, bitrate=None) -> Spec | None:
        m = self._want
        if not m:
            return None
        ds = win32.displays()
        d = ds[min(max(0, int(m.get("display", 0))), len(ds) - 1)]
        mode = m.get("mode") if m.get("mode") in MODES else "h264"
        if self._use_alt and m.get("alt") in MODES:
            mode = m["alt"]  # the link couldn't carry the preferred mode
        if mode != "jpeg" and not encoders(MODES[mode][0]):
            mode = "h264" if mode == "hevc" and encoders("h264") else "jpeg"
        mjpeg = mode == "jpeg"
        t = tuning(float(m.get("q", 0.5)))
        if mjpeg:
            # Adaptive JPEG quality (higher q:v = smaller frames), and never
            # forced to native size: a 2560px JPEG is ~200 KB per frame.
            t = {**t, "jpeg": min(14, t["jpeg"] + self._j["pen"]), "native": False}
        fps = max(10, min(int(m.get("fps", 60)), d["hz"], t["fps_cap"], 240))
        rw, rh = max(64, int(m.get("w", 1280)) * t["scale"]), max(64, int(m.get("h", 720)) * t["scale"])
        s = min(1.0, rw / d["w"], rh / d["h"])
        if t["native"] or s >= (0.95 if mjpeg else 0.8) or not mjpeg and s >= 0.66 and fps <= 60:
            w, h = d["w"], d["h"]  # near native: skip scaling (zero-copy, 0% CPU)
        else:
            w, h = max(2, int(d["w"] * s) & ~1), max(2, int(d["h"] * s) & ~1)
        enc = ("tiles" if tiles.available() and time.monotonic() - _bad_encoders.get("tiles", -1e9) > 60
               else "mjpeg") if mjpeg else encoders(MODES[mode][0])[0]
        if fps > 120 and enc != "tiles" and not ((w, h) == (d["w"], d["h"]) and enc.endswith("_nvenc")):
            # Shrinking runs on the CPU here (~1 core per 100 fps). With NVENC,
            # encode at native size instead: zero-copy, the GPU carries the
            # extra frames. (Tiled JPEG shrinks on the GPU: no limit.)
            if enc.endswith("_nvenc"):
                w, h = d["w"], d["h"]
            else:
                fps = 120
        spec = Spec(mode, d, w, h, fps, encoder=enc, t=t)
        if mjpeg:
            return spec
        if bitrate is None:
            # Start from the quality level's target (generous for a LAN; the
            # congestion control walks it down fast), within measured capacity.
            bitrate = self._bitrate or max(4e6, min(60e6, w * h * fps * t["bpp"]))
            with self._cv:
                bw = max((b for _, b in self._bw), default=0)
            if bw and not self._bitrate:
                bitrate = min(bitrate, bw * 0.75)
        spec.bitrate = int(max(MIN_BITRATE, min(bitrate, self._max_bitrate)))
        return spec

    def _reconfigure(self):
        spec = self._target_spec()
        if spec is None:
            return
        cur = self._next or self.enc
        if cur and cur.spec.key() == spec.key():
            return
        self._switch(spec=spec, reason="config")

    def _switch(self, spec=None, flush=False, reason=""):
        """Start a new encoder. flush=True drops everything queued for the
        old one right away (used when the link is badly congested or the
        decoder needs a keyframe); otherwise the old encoder keeps
        streaming until the new one delivers its first keyframe."""
        # Serialized: the sender (stats tick), the WS reader (ACKs, cfg) and
        # ffmpeg exit handlers can all decide to switch at the same moment.
        with self._switch_lock:
            spec = spec or self._target_spec()
            if spec is None:
                return
            now = time.monotonic()
            cur = self._next or self.enc
            if (isinstance(cur, tiles.TileEncoder) and cur.alive and spec.encoder == "tiles"
                    and cur.spec.display is not None and spec.key()[:5] == cur.spec.key()[:5]):
                # Same display and picture size: new quality / fps apply in
                # place, without resending the whole screen.
                cur.retune(spec)
                if flush:
                    cur.refresh()
                with self._cv:
                    self._last_switch, self._last_reason = now, reason
                return
            self._restarts.append(now)
            while self._restarts and now - self._restarts[0] > 10:
                self._restarts.popleft()
            with self._cv:
                if self._closed:
                    return
                if self._next:
                    self._next.stop()
                self._gen += 1
                self._last_switch = now
                self._last_reason = reason
                if spec.mode != "jpeg":
                    self._bitrate = spec.bitrate
                self._qsamples.clear()
                if flush:
                    self._flushing = True
                    self._queue.clear()
                    self._inflight.clear()
                cls = tiles.TileEncoder if spec.encoder == "tiles" else Encoder
                enc = self._next = cls(spec, self._gen, self._on_packet, self._on_exit)
                enc.reason = reason
            try:
                enc.start()
            except OSError as exc:
                global FFMPEG
                FFMPEG = find_ffmpeg()  # a winget upgrade moves the versioned folder
                with self._cv:
                    if self._next is enc:
                        self._next = None
                        self._flushing = False
                self.error = str(exc)
                self.ws.send_json({"t": "error", "msg": f"ffmpeg failed to start: {exc}"})

    def _promote(self, enc: Encoder):
        # Called under _cv when `enc` produced its first frame.
        old, self.enc, self._next = self.enc, enc, None
        self._flushing = False
        if enc.spec.codec == "mjpeg":  # no codec config in-band; still tell the client what it gets
            self._configs[enc.gen] = {"t": "config", "gen": enc.gen & 0xFFFF, "mode": "jpeg",
                                      "w": enc.spec.w, "h": enc.spec.h, "fps": enc.spec.fps,
                                      "jq": enc.spec.t["jpeg"], "reason": enc.reason,
                                      "tiles": enc.spec.encoder == "tiles"}
        self._queue = deque(f for f in self._queue if f[0] == enc.gen)
        self._latest = None
        self._last_jpeg = None
        self._qsamples.clear()
        # Keep only ~30ms of video in the kernel send buffer: a backlog must
        # sit in our queue, where a flush can drop it, not in the OS.
        br = enc.spec.bitrate or 40_000_000
        try:
            self.ws.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF,
                                    int(max(64 << 10, min(1 << 20, br / 8 * 0.03))))
        except (OSError, AttributeError):
            pass
        if old:
            old.stop()

    # ---- encoder callbacks (ffmpeg reader threads) ----------------------
    def _on_packet(self, enc: Encoder, kind, payload, key):
        with self._cv:
            if self._closed or (enc is not self._next and enc is not self.enc):
                return
            if kind == "config":
                self._configs[enc.gen] = {**self._config_msg(enc.spec, enc.gen, payload), "reason": enc.reason}
                return
            if enc is self._next:
                if kind == "frame" and not key:
                    return  # can't start a stream mid-GOP
                self._promote(enc)
            elif self._flushing:
                return  # frames were dropped: the old stream can't be decoded past the gap
            if kind == "tiles":
                # New content (or a still area due for its sharper copy): the
                # sender builds the message from the newest pixels when the
                # link has room; changes pile up in the meantime.
                if payload == "change":
                    self._j["prod"] += 1
                    if self._latest is not None:
                        self._j["skip"] += 1
                self._latest = (enc.gen, enc, now_ms())
            elif kind == "jpeg":
                if payload == self._last_jpeg:
                    return  # screen unchanged
                self._last_jpeg = payload
                j = self._j
                j["prod"] += 1
                if self._latest is not None:
                    j["skip"] += 1  # superseded before the link had room for it
                j["size"] = len(payload) if not j["size"] else 0.9 * j["size"] + 0.1 * len(payload)
                self._latest = (enc.gen, payload, now_ms())
            else:
                kind_id = KIND_FMP4 if enc.spec.container == "fmp4" else KIND_FRAME
                self._queue.append((enc.gen, key, payload, now_ms(), kind_id))
            self._cv.notify_all()

    @staticmethod
    def _config_msg(spec: Spec, gen: int, rec: bytes) -> dict:
        """What the client needs to set up its decoder for a new stream."""
        msg = {"t": "config", "gen": gen & 0xFFFF, "mode": spec.mode, "w": spec.w, "h": spec.h,
               "fps": spec.fps, "encoder": spec.encoder, "bitrate": spec.bitrate, "native": spec.native,
               "cq": spec.t["cq"], "q": spec.t["q"]}
        if spec.container == "fmp4":  # rec = init segment; the avcC box inside names the codec
            i = rec.find(b"avcC")
            a = rec[i + 4:i + 8] if i >= 0 else b"\x01\x64\x00\x28"
            msg["codec"] = f"avc1.{a[1]:02x}{a[2]:02x}{a[3]:02x}"
            msg["init"] = base64.b64encode(rec).decode()
        else:
            msg["codec"] = hevc_codec_string(rec) if spec.codec == "hevc" else f"avc1.{rec[1]:02x}{rec[2]:02x}{rec[3]:02x}"
            msg["desc"] = base64.b64encode(rec).decode()
        return msg

    def _on_exit(self, enc: Encoder):
        # ffmpeg died (display mode change, lock screen, driver reset, or a
        # broken encoder). Retry, falling back to the next encoder if this
        # one never produced a frame.
        err = " | ".join(enc.errors) or "ffmpeg exited"
        low = err.lower()
        # A lock screen / UAC prompt / mode change breaks *capture*; only
        # blame the encoder when ffmpeg says the encoder failed.
        capture = any(w in low for w in ("ddagrab", "dxgi", "duplicat", "access denied", "e_accessdenied"))
        if enc.spec.encoder == "tiles" and enc.first_frame is None:
            _bad_encoders["tiles"] = time.monotonic()  # in-process capture failed: ffmpeg MJPEG for a while
        if (enc.first_frame is None and enc.spec.mode != "jpeg" and not capture
                and ("encod" in low or enc.spec.encoder.split("_")[-1] in low)):
            if enc.spec.encoder in encoders(enc.spec.codec) and len(encoders(enc.spec.codec)) > 1:
                _bad_encoders[enc.spec.encoder] = time.monotonic()
        with self._cv:
            if self._closed or (enc is not self.enc and enc is not self._next):
                return
            if enc is self._next:
                self._next = None
                # A failed switch: keep the stream that is still playing,
                # unless we already dropped frames for it (flush).
                if self.enc and self.enc.alive and not self._flushing:
                    self._last_switch = time.monotonic()  # back off probing
                    return
            else:
                self.enc = None
        self.ws.send_json({"t": "error", "msg": err[-300:], "retry": True})
        time.sleep(1.0 if len(self._restarts) < 5 else 3.0)
        if not self._closed:
            self._switch(flush=True, reason="encoder exit")

    # ---- sending ---------------------------------------------------------
    def _pace_wait(self) -> float:
        """Latest-wins JPEG: seconds to hold the next frame so the send rate
        stays under ~75% of measured link capacity (0 = send now). A full
        Wi-Fi link would also delay the phone's own input and ACKs, since
        both directions share the airtime. Called under _cv."""
        t = now_ms()
        # Recent samples only: after the link drops, a 5s-old peak would
        # keep us flooding it.
        bw = max((b for ts, b in self._bw if t - ts < 2000), default=0)
        while self._sent_log and t - self._sent_log[0][0] > 250:
            self._sent_log.popleft()
        # the link's room, and the viewer's own bitrate limit (if any)
        rate = min(bw * 0.75 if bw else float("inf"), self._max_bitrate if self._max_bitrate < MAX_BITRATE else float("inf"))
        if rate == float("inf") or sum(b for _, b in self._sent_log) * 8 < rate * 0.25:
            return 0.0
        return max(0.002, (250 - (t - self._sent_log[0][0])) / 1000)

    def _jpeg_slots(self) -> int:
        """Frames allowed in flight. Two keep the link busy while an ACK is
        on its way back; but once a frame takes long to deliver, the second
        just waits behind the first and doubles the latency. Under _cv."""
        return 1 if self._j["delay"] > 8 else 2

    def _want_probe(self, now) -> bool:
        # Capacity samples come from big frames. On the H.264 fallback a quiet
        # screen sends none, so we'd never learn the link got better.
        with self._cv:
            fresh = any(now_ms() - ts < 10_000 for ts, _ in self._bw)
        return now - self._last_probe > 10 and (self._use_alt or not fresh) and bool(self.enc)

    def _probe(self) -> bool:
        """A 32 KB padding message the client ACKs and ignores: its ACK delay
        is one packet-train capacity sample."""
        self._last_probe = time.monotonic()
        with self._cv:
            self._seq = (self._seq + 1) & 0xFFFFFFFF
            seq = self._seq
            self._inflight[seq] = (now_ms(), PROBE_BYTES, KIND_PROBE)
        return self.ws.send(_HDR.pack(KIND_PROBE, 0, 0, seq, now_ms()) + bytes(PROBE_BYTES))

    def _send_loop(self):
        last_stats = time.monotonic()
        while True:
            with self._cv:
                item = None
                while not self._closed:
                    if self._latest and self._inflight:
                        # A lost ACK must not stall latest-wins forever.
                        t_old = now_ms() - 1500
                        for s_ in [s_ for s_, v in self._inflight.items() if v[0] < t_old]:
                            self._inflight.pop(s_)
                    if self._queue:
                        item = self._queue.popleft()
                        break
                    if self._latest and len(self._inflight) < self._jpeg_slots():
                        wait = self._pace_wait()
                        if wait <= 0:
                            item, self._latest = self._latest, None
                            break
                        # time.sleep is high-resolution; Condition.wait on
                        # Windows rounds up to the ~15.6 ms system tick.
                        self._cv.release()
                        try:
                            time.sleep(wait)
                        finally:
                            self._cv.acquire()
                        continue
                    if time.monotonic() - last_stats > 1.0:
                        break
                    self._cv.wait(0.25)
                if self._closed:
                    return
                if item is not None and self.enc and item[0] != self.enc.gen:
                    continue  # stale generation
                cfg = None
                if item is not None and item[0] != self._sent_cfg_gen and item[0] in self._configs:
                    cfg = self._configs[item[0]]
                    self._sent_cfg_gen = item[0]
                if item is not None:
                    self._seq = (self._seq + 1) & 0xFFFFFFFF
                    seq = self._seq
            if cfg and not self.ws.send_json(cfg):
                return
            if item is not None:
                if len(item) == 5:
                    gen, key, data, t, kind = item
                    flags = 1 if key else 0
                elif isinstance(item[1], tiles.TileEncoder):
                    gen, enc, _ = item
                    built = enc.take()  # encodes now, from the newest pixels
                    if built is None:
                        continue
                    data, t, full, more = built
                    kind, flags = KIND_TILES, 1 if full else 0
                    with self._cv:
                        j = self._j
                        j["size"] = len(data) if not j["size"] else 0.9 * j["size"] + 0.1 * len(data)
                        if more and self._latest is None:
                            self._latest = (gen, enc, now_ms())  # more still areas to sharpen
                else:
                    gen, data, t = item
                    kind, flags = KIND_JPEG, 1
                with self._cv:
                    self._inflight[seq] = (now_ms(), len(data), kind)
                    self._sent_log.append((now_ms(), len(data)))
                if not self.ws.send(_HDR.pack(kind, flags, gen & 0xFFFF, seq, t) + data):
                    return
            now = time.monotonic()
            if now - last_stats > 1.0:
                # Tiny round trip: pure propagation delay for the capacity estimate.
                self.ws.send_json({"t": "sping", "s": now_ms()})
                if self._want_probe(now) and not self._probe():
                    return
            if now - last_stats > 1.0:
                last_stats = now
                self._control(stats=True)

    # ---- congestion control ---------------------------------------------
    def _on_ack(self, seq):
        t = now_ms()
        with self._cv:
            sent = self._inflight.pop(seq, None)
            # Frames older than an acked one were delivered too (TCP is in order).
            for s in [s for s in self._inflight if (seq - s) & 0xFFFFFFFF < 0x7FFFFFFF]:
                self._inflight.pop(s)
            if sent:
                rtt = t - sent[0]
                self._acked.append((t, sent[1]))
                self._rtts.append((t, rtt))
                while self._rtts and t - self._rtts[0][0] > 10_000:
                    self._rtts.popleft()
                base = min(r for _, r in self._rtts)
                q = max(0.0, rtt - base)
                self._qdelay = 0.8 * self._qdelay + 0.2 * q
                self._qsamples.append((t, q))
                while self._qsamples and t - self._qsamples[0][0] > 150:
                    self._qsamples.popleft()
                # Packet-train capacity estimate: a big frame's extra delay
                # over a tiny frame's is its serialization time on the link.
                if sent[1] < 3000:
                    self._small_rtts.append((t, rtt))
                while self._small_rtts and t - self._small_rtts[0][0] > 10_000:
                    self._small_rtts.popleft()
                prop = min([r for _, r in self._small_rtts] + [r for _, r in self._prop] or [base])
                if sent[2] in (KIND_JPEG, KIND_TILES):  # send -> fully received, minus propagation
                    self._j["delays"].append((t, max(0.0, rtt - prop)))
                if sent[1] >= 16_000 and rtt - prop > 0.5:
                    self._bw.append((t, sent[1] * 8 / ((rtt - prop) / 1000)))
                while self._bw and t - self._bw[0][0] > 5000:
                    self._bw.popleft()
            self._cv.notify_all()
        self._control()

    def _control(self, stats=False):
        enc = self.enc
        if not enc or self._next:
            if stats:
                self._send_stats()
            return
        now = time.monotonic()
        if enc.spec.mode == "jpeg":
            if stats:  # JPEG adapts once a second (pacing does the fast part)
                self._control_jpeg(now)
                self._send_stats()
            return
        t = now_ms()
        with self._cv:
            while self._acked and t - self._acked[0][0] > 1000:
                self._acked.popleft()
            delivered = sum(b for _, b in self._acked) * 8  # bits in last second
            inflight = len(self._inflight) + len(self._queue)
            qd = self._qdelay
            # Persistent queueing = the *minimum* delay over the last 150ms;
            # a draining backlog or one big keyframe doesn't count.
            persist = min(q for _, q in self._qsamples) if len(self._qsamples) >= 3 else 0.0
        while self._restarts and now - self._restarts[0] > 10:
            self._restarts.popleft()
        fps = enc.spec.fps
        frame_ms = 1000 / fps
        br = self._bitrate
        if self._use_alt and now - self._alt_since > self._alt_retry and now - self._last_switch > 10:
            with self._cv:
                bw = max((b for ts, b in self._bw if t - ts < 15_000), default=0)
            # Capacity samples are optimistic (bursts pass instantly), so they
            # only gate the *attempt*; JPEG's measured delay then keeps it or
            # sends us back here (with a longer wait before the next try).
            size = self._jpeg_size / 0.85  # one step sharper than where we gave up
            if not bw or (size * 8 / bw * 1000 <= JPEG_TX_MS * 0.7 and bw * 0.75 >= size * 8 * fps):
                self._use_alt = False
                self._jpeg_since = now
                self._j["pen"] = max(0, self._j["pen"] - 2)
                self._switch(reason="probe JPEG")
                return
        severe = persist > 250 or inflight > fps
        congested = persist > max(25, 2 * frame_ms) or inflight > max(4, fps * 0.25)
        since_switch = now - self._last_switch
        if severe and since_switch > 0.8:
            self._note_congestion(now)
            self._switch(self._target_spec(bitrate=max(MIN_BITRATE, min(br * 0.6, delivered * 0.7))),
                         flush=True, reason="severe congestion")
        elif congested:
            self._congested_since = self._congested_since or now
            if now - self._congested_since > 0.15 and since_switch > 1.0:
                self._congested_since = None
                self._note_congestion(now)
                with self._cv:
                    bw = max((b for _, b in self._bw), default=0)
                if self._last_reason == "probe" and since_switch < 10:
                    target = self._pre_probe  # the probe overshot: back to known-good
                else:
                    target = max(MIN_BITRATE, min(br * 0.8, delivered * 0.85, bw * 0.7 if bw else 1e12))
                self._switch(self._target_spec(bitrate=target), reason="congestion")
        else:
            self._congested_since = None
            # Probe upward when the encoder is actually using its budget (so
            # quality is being capped) and the link shows no queueing. Stay
            # under a rate that recently congested; the hold doubles each
            # time the same ceiling is hit again.
            busy = delivered > 0.55 * br
            limit = self._max_bitrate
            with self._cv:
                bw = max((b for _, b in self._bw), default=0)
            if self._ceiling and bw > self._ceiling[0] * 1.6:
                self._ceiling = None  # measured capacity says the link got better
            if self._ceiling and now - self._ceiling[1] < self._ceiling_hold:
                limit = min(limit, self._ceiling[0] * 0.92)
            if bw:
                # Headroom for bursts: at ~90% utilisation a single keyframe
                # queues for hundreds of ms (it drains at the spare 10%).
                limit = min(limit, bw * 0.75)
            # Step up 25%, or jump toward 60% of measured capacity if higher.
            target = min(max(br * 1.25, bw * 0.6), limit)
            if (busy and persist < 8 and since_switch > 4 and target > br * 1.05
                    and len(self._restarts) < 4):
                self._pre_probe = br
                self._switch(self._target_spec(bitrate=target), reason="probe")
        if stats:
            self._send_stats(delivered, qd, inflight)

    def _control_jpeg(self, now):
        """JPEG stays while each frame reaches the phone quickly. The measure
        is each frame's delivery delay (send -> ACK, minus propagation): it
        can't be fooled by bursts the way a capacity estimate can. Too slow:
        make frames lighter if that can fix it, else hand over to the
        alternate mode (the H.264 player)."""
        j = self._j
        t = now_ms()
        with self._cv:
            prod, skip = j["prod"], j["skip"]
            j["prod"] = j["skip"] = 0
            recent = sorted(d for ts, d in j["delays"] if t - ts < 1000)
        if recent:
            j["delay"] = recent[len(recent) // 2]
        if prod < 15 or len(recent) < 5:
            return  # screen mostly still: no evidence either way
        delay = j["delay"]
        good = delay <= JPEG_TX_MS and skip <= 0.35 * prod
        roomy = delay <= JPEG_TX_MS / 2 and skip <= 0.1 * prod
        j["lim"] = 0 if good else j["lim"] + 1
        j["free"] = j["free"] + 1 if roomy else 0
        since = now - self._last_switch
        alt_ok = (self._want or {}).get("alt") and not self._use_alt
        if j["lim"] >= 2 and since > 2:
            j["lim"] = 0
            # Each +2 on q:v trims frames ~15%; only step down if that can
            # plausibly bring delivery under the latency budget.
            if j["pen"] < 6 and (delay * 0.85 <= JPEG_TX_MS or not alt_ok):
                j["pen"] += 2
                self._switch(reason="slow link: lighter JPEG")
            elif alt_ok:
                self._jpeg_size = j["size"]
                attempt = self.enc.reason == "probe JPEG" and now - self._jpeg_since < 20
                # a failed return attempt backs off; a long JPEG spell resets it
                self._alt_retry = min(300.0, self._alt_retry * 2) if attempt else 20.0
                self._use_alt, self._alt_since, self._bitrate = True, now, 0
                self._switch(reason="probe failed" if attempt else "slow link: switched to H.264")
        elif j["free"] >= 8 and j["pen"] > 0 and since > 8:
            j["free"] = 0
            j["pen"] -= 1
            self._switch(reason="link recovered: sharper JPEG")

    def _note_congestion(self, now):
        repeat = self._ceiling and now - self._ceiling[1] < 60
        self._ceiling_hold = min(120, self._ceiling_hold * 2) if repeat else 30.0
        self._ceiling = (self._bitrate, now)

    def _send_stats(self, delivered=None, qd=None, inflight=None):
        enc = self.enc
        if not enc:
            return
        up = time.monotonic() - enc.started
        self.ws.send_json({"t": "stats", "enc": enc.spec.encoder or "mjpeg", "mode": enc.spec.mode,
                           "cq": enc.spec.t["cq"], "preset": enc.spec.t["preset"], "w": enc.spec.w,
                           "h": enc.spec.h, "fps": enc.spec.fps, "cap": enc.spec.bitrate,
                           "rate": delivered, "qd": None if qd is None else round(qd, 1),
                           "inflight": inflight, "native": enc.spec.native, "jq": enc.spec.t["jpeg"],
                           "alt": self._use_alt,
                           "startup_ms": round(1000 * ((enc.first_frame or time.monotonic()) - enc.started)),
                           "avg_fps": round(enc.frames / up, 1) if up > 0 else 0})
