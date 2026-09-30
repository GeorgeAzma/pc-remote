# PC Remote

Control your Windows PC from your phone: a live, low-latency view of the
screen with a trackpad and keyboard, a real terminal, and one-tap power and
media controls. It's a small Python server on `0.0.0.0:1024`; open it in any
browser on the same network (or over Tailscale).

- **Remote**: the PC screen at up to 120 fps (240 on the zero-copy path),
  typically 5–20 ms from screen change to your phone. Pinch the picture to
  zoom (the stream gets sharper as you zoom in), tap it to click, or use the
  trackpad below it.
- **Terminal**: PowerShell / CMD / WSL with colors, line editing and
  full-screen apps. Sessions survive tab switches, reconnects and phone
  locks.
- **Controls**: sleep, lock, screen off, screenshot, media keys, volume and
  brightness sliders, Wi-Fi/Bluetooth, open app, open link, send files
  either way, clipboard sync, running apps, and timed shutdown/restart.
  Sleep, lock and power actions take two taps (the first turns the button
  red); press and hold them for timers. The header shows CPU, RAM, GPU and
  the live round trip to the PC; tap it to open the System tab. **About**
  at the bottom has the PC's details, its addresses (with copy buttons),
  the certificate install guide and how to install the remote as an app.
- **System**: every core's load and clock (performance cores drawn bigger
  than efficiency cores), memory, GPU (VRAM, temperature, power, clocks,
  fan, encoder), disks, network and the busiest apps, with a minute of
  history. It updates 20 times a second (pushed over a WebSocket; the
  busiest apps once a second), and the server only samples while it's open.
  While it's showing, the phone's screen stays on (on the HTTPS address), so
  it works as a second screen.

## Setup

```bat
winget install Gyan.FFmpeg
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
launch_remote.bat
```

Then open `http://<PC-IP>:1024/` on your phone. Controls → About → *Install
as app* shows how to add it to your home screen, so it opens full-screen.

**HD stream (recommended).** Browsers only allow the lowest-latency H.264
decoder (WebCodecs) on secure pages. Over plain `http://` the stream uses
tiled JPEG: only the parts of the screen that changed are sent, so it's
just as quick and light for typical use, but a full-screen video costs far
more bandwidth than H.264. It switches itself to H.264 in a video player
(30–50 ms slower) when the link is too slow for JPEG. Tap **HD** on the screen, or open
`https://<PC-IP>:1024/`, to get both low latency and low bandwidth. The server makes
its own certificate, so accept the warning once. To get rid of the warning
for good, install it as a trusted CA: Controls → About → *Secure connection*
walks you through it for iPhone/iPad, Android, Windows and Mac (and shows
the fingerprint to check). The file itself is at `/ca.crt`.

**Token.** Set `PC_API_TOKEN` in `launch_remote.bat` so random devices on
your network can't control your PC. Open `/?token=YOUR_TOKEN` once; after
that the browser remembers it (the token is moved out of the address bar).

Without a token, other websites still can't use the server through your
browser:

- The server refuses cross-site requests and WebSockets (it checks
  `Sec-Fetch-Site` and `Origin`), so a page you visit can't call
  `/shutdown` or open a terminal.
- It only answers to IPs, `localhost` and this PC's own name, which blocks
  DNS rebinding. That includes names like a Tailscale MagicDNS
  `pc.tailnet.ts.net`. If you use another name, list it in
  `PC_ALLOWED_HOSTS` (comma-separated).

Scripts and curl are unaffected.

## Trackpad

| Gesture | Action |
| --- | --- |
| tap | left click (tap twice = double click) |
| two-finger tap | right click |
| three-finger tap | middle click |
| hold, or tap then drag | drag (hold the left button) |
| two-finger drag | scroll, with momentum |
| three-finger swipe ↑ / ↓ / ←→ | task view / show desktop / switch app |
| tap on the screen image | click exactly there (or use it as a trackpad, see Settings) |
| pinch on the screen image | zoom in (the stream switches to higher resolution) |

Drag the grab bar between the picture and the panel to resize the panel
(the trackpad's height on a phone in portrait); drag it on past its smallest
size to hide it. For watching videos, tap the bar: the panel (trackpad, keys
and text field) slides away and the browser goes full screen. The bar then
waits at the screen edge; tap it, drag it out, or press Esc to bring the
panel back.

Switch between Remote, Terminal, Controls and System with the small icons at
the top of each view: over the picture, in the terminal's bar and in the
Controls and System headers. They take no room of their own.
The picture still takes taps and your physical keyboard (or works as the
trackpad, see *Touching the screen* in Settings).

Pointer speed, acceleration, scroll speed and direction are in ⚙ Settings,
along with *Gestures & keys*, an illustrated guide to all of these.
Pointer motion uses a velocity curve: slow strokes are precise, and a quick
flick crosses the whole screen.

**Keyboard.** Whatever you type in the *Type on PC* field goes to the PC as
you type, including autocorrect, swipe typing and dictation. ⌘ opens a
searchable list of shortcuts, keys (Esc, arrows, F-keys, …) and modifiers,
where you can also type any combo such as `ctrl+shift+esc`. The key bar
above the text field starts empty: tap the pin next to anything in that
list to add it, and hold a key in the bar to remove it. Pinned `Ctrl`,
`Alt`, `⇧` and `⊞` are sticky: tap one, then a key (tap twice to lock it).
On a computer, click the screen image to send your physical keyboard
straight to the PC.

## How the stream works

```
ddagrab (GPU desktop duplication)
  ├─ native size: D3D11 texture → NVENC           zero-copy, ~0% CPU
  └─ smaller:     download → swscale → NVENC      ≈ 1 core per 100 fps
NVENC H.264: ultra-low-latency, no B-frames, infinite GOP, constant quality with a max-bitrate cap
  → FLV on stdout (length-prefixed, so a frame is sent the moment it's complete)
  → WebSocket → browser WebCodecs decoder → low-latency canvas
```

- The resolution matches the picture's size on your screen in device
  pixels (times the zoom level), capped at the monitor's size. The frame
  rate follows your display's refresh rate (up to 240). Above 120 fps a
  shrunk H.264 picture would cost a CPU core per 100 fps, so NVENC then
  encodes at native size instead (zero-copy, on the GPU).
- Every frame is acknowledged. Queueing delay, measured as the minimum
  over 150 ms, and a per-frame capacity estimate drive the bitrate down
  fast when the link is saturated, and back up when it recovers. The
  bitrate stays near 75% of measured capacity, so bursts like keyframes
  don't queue.
- Changing bitrate or resolution starts a second encoder in parallel and
  switches over at its first keyframe, so the picture never pauses.
- The mouse cursor isn't part of the video. The phone draws it and moves it
  instantly from your own finger movements, and the PC's real position
  corrects it.
- A static screen costs about 100 bytes per frame, and the encoder keeps
  sharpening it in the meantime.
- Other GPUs: the server falls back to QSV, AMF, or libx264.

**Stream method** (⚙ on the Remote tab):

| Method | Needs | Latency | Bandwidth |
| --- | --- | --- | --- |
| H.264 (Automatic on HTTPS) | HTTPS | lowest | 1× |
| HEVC | HTTPS + HEVC decoder | lowest | ~0.7× |
| Video player (MSE) | any modern browser | +30–50 ms | 1× |
| JPEG (Automatic on HTTP) | anything | lowest on a fast link | small changes: ~1×; full-screen motion: ~10× |

**Tiled JPEG.** The server captures the screen itself (DXGI desktop
duplication) and shrinks it to the stream size on the GPU. Windows reports
which areas changed; the server treats that as a hint and compares those
32×32-pixel tiles with the previous frame, so what it sends is exact (plus a
full comparison once a second, in case a report was missed).

- Only the changed tiles are sent, merged into a few rectangles, one small
  JPEG each; the phone paints them over its copy of the screen.
- When more than half the screen changed, it sends the whole screen instead,
  as 4 horizontal JPEGs: browsers decode separate images in parallel, so a
  full frame decodes about 2.5× faster than one big JPEG (1.6 vs 4.0 ms in
  Chrome), for ~3% more data.
- Changes pile up while the link is busy and go out together, encoded from
  the newest capture, so nothing queues and nothing is lost.
- An area that has been still for 0.3 s is resent at a higher quality, so
  still content ends up sharper than moving content.

Measured with a 1168×658 viewer, a small animation in a screen corner:
3–4 Mb/s and 1–2 ms latency (whole-screen JPEG: ~30 Mb/s), and it stayed
on JPEG down to a 6 Mb/s link. For full-screen motion it matches the plain
JPEG stream in bandwidth, decodes faster, and uses about a third of the
CPU. It needs `numpy` and `simplejpeg`; without them, or with
`PC_JPEG_TILES=0`, JPEG uses ffmpeg (a whole JPEG per frame).

**Adaptive JPEG.** JPEG is paced to about 75% of the link's measured
capacity, so it never floods the Wi-Fi; a full link would also slow down
your trackpad input. The server times how long each frame takes to reach
the phone, from its ACK minus the network's round-trip time:

- It stays on JPEG while frames arrive within 20 ms.
- If they're slower, it first makes frames lighter.
- If that can't help, Automatic switches to the H.264 player.
- It tries JPEG again later, waiting longer after each failed try.

In a simulated-link test with whole-screen JPEG frames (full-screen motion,
or the ffmpeg fallback), the median delivery delay for a phone-sized
picture was:

| Link | Stream | Delivery delay (median) |
| --- | --- | --- |
| 45 Mb/s | JPEG, full quality | 2 ms |
| 25 Mb/s | JPEG, slightly lighter | 7 ms |
| 12 Mb/s | H.264, within ~2 s of the drop | under 1 ms (the player adds 30–50 ms) |

**Speed ↔ Quality** slider, five steps:

| Step | Resolution | Detail | Encoder preset | Frame size cap |
| --- | --- | --- | --- | --- |
| Fastest | ¾ of on-screen size | CQ 27 | p1 | ~1 frame |
| Balanced | on-screen size | CQ 22 | p4 | ~3 frames |
| Sharpest | always native | CQ 16 | p6 | ~8 frames |

A frame is never allowed to be larger than a few frame-times of bitrate,
so a burst can't pile up delay. The steps in between interpolate.
Changing the slider switches encoders seamlessly while you watch. Every step
runs at the full frame rate: the lower of your screen's and the PC
monitor's refresh rate (more frames always look better). The bitrate
follows the link, using as much of it as it can. **Max bitrate** caps that,
e.g. on mobile data; it's off (no limit) by default. For JPEG the slider
sets the JPEG quality instead, and the cap limits how fast frames go out,
so the quality steps down to fit.

## Adding commands

Every action is a decorated function in `commands.py`. It becomes an
endpoint (`GET/POST /<name>?param=value`) and shows up in the UI:

```python
@command("say", "Speak text on the PC.", tab="tools", icon="volume")
def say(text: str = ""):
    ...
    return {"status": "spoken"}
```

`GET /api/commands` lists all commands. Hidden API-only helpers include
`/type?text=`, `/keys?combo=ctrl+c`, `/mousemove?dx=&dy=`, `/stats` and
`/processes`.

## Files

| File | Purpose |
| --- | --- |
| `main.py` | HTTP/HTTPS server, routing, auth, uploads/downloads |
| `commands.py` | `@command` registry and every PC action |
| `sysmon.py` | live system monitor: per-core CPU and topology, memory, GPU, disks, network |
| `video.py` | ffmpeg capture/encode pipeline, per-viewer rate control |
| `tiles.py` | tiled JPEG: change tracking, rectangles, refinement |
| `dxgicap.py` | ctypes DXGI desktop duplication and GPU shrinking |
| `remote.py` | input WebSocket, cursor and clipboard broadcast |
| `terminal.py` | persistent ConPTY shell sessions |
| `win32.py` | ctypes: input injection, clipboard, cursor shapes, displays, audio, stats |
| `wsock.py` | minimal WebSocket implementation |
| `certs.py` | local CA and server certificate for HTTPS |
| `web/` | the app: `remote.js`, `term.js` (VT emulator), `controls.js`, `app.js` |

## Run at Windows startup (elevated)

Scheduled Tasks can start the server with admin rights and no UAC prompt.
You only need admin for a few things: typing into elevated windows, and the
Wi-Fi fallback when the radio API isn't available.

1. `Win+R` → `taskschd.msc` → **Create Task…**
2. **General**: name it `PC Remote`, tick **Run with highest privileges**,
   and keep **Run only when user is logged on**. The other option runs in
   session 0, which has no desktop, so screen capture and input can't work.
3. **Triggers** → New → **At log on** → your account.
4. **Actions** → New → Start a program → `C:\code\startup\launch_remote.bat`,
   *Start in* `C:\code\startup`.
5. **Conditions**: untick *Start the task only if the computer is on AC power*.

Without admin: put a shortcut to `launch_remote.bat` in `shell:startup`.

Errors are written to `server.log` when running headless via `pythonw`.
