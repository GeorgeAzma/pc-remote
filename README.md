# PC Remote

![PC Remote on a phone: Remote, Terminal, Controls and System](docs/screenshots/hero.webp)

Your Windows PC, from your phone's browser. No app, no account, no cloud.

- **Remote**: the live screen at up to 240 fps, 5–20 ms behind the real one. Trackpad, keyboard, pinch to zoom.
- **Terminal**: PowerShell, CMD or WSL, full color, survives reconnects.
- **Controls**: sleep, lock, media, volume, brightness, Wi-Fi, Bluetooth, open apps, send files, clipboard sync, power timers.
- **System**: every core, GPU, memory, disks and network, 20 updates a second.

![The Remote tab in a desktop browser](docs/screenshots/desktop-remote.webp)
![The System tab in a desktop browser](docs/screenshots/desktop-system.webp)

## Install

Run **PC-Remote-Setup.exe** from [Releases](https://github.com/GeorgeAzma/pc-remote/releases). That's it.

It starts with Windows (with admin rights, no UAC prompt) and opens the firewall on home networks and Tailscale only. It then shows a QR code: scan it with your phone and you're in.

Away from home? Put the PC and the phone on [Tailscale](https://tailscale.com) and choose the Tailscale QR code.

## Sign-in

Sign-in is **on** by default. A new device scans the PC's QR code (with its camera, or **Scan the QR code** in the app) or enters the access code, once. The PC itself never has to (WSL, containers and tunnels on it do). Manage it in **Controls → About → Sign-in & devices**, where you can:

- add a device with a QR code;
- use your own password instead of the code;
- sign out every other device;
- turn sign-in off, if your network is already private (Tailscale, say). This setting is saved.

Either way, websites you visit can't reach the server through your browser: it blocks cross-site requests and DNS rebinding.

## HD stream

Over `http://` the stream is tiled JPEG, which sends only the parts of the screen that changed. Over `https://` you also get H.264 or HEVC through WebCodecs, which uses far less bandwidth for full-screen video. Tap **HD** on the screen. To get rid of the certificate warning, **Controls → About → Secure connection** shows how to trust the certificate on each platform.

## How it's fast

- **Tiled JPEG**: DXGI desktop duplication, a GPU downscale, and only the changed 32×32 tiles are sent. Still areas are resent sharper.
- **H.264 / HEVC**: NVENC with zero copies, no B-frames, and a frame is sent the moment it's encoded. It falls back to QSV, AMF or x264 on other GPUs.
- **Rate control**: every frame is acknowledged. The bitrate follows the link at about 75% of its capacity, so nothing queues. Quality and resolution changes swap encoders without a pause.
- **The cursor** is drawn on the phone and moves as soon as your finger does.

## From source

```bat
winget install Gyan.FFmpeg
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
launch_remote.bat
```

## Build the installer

```bat
.venv\Scripts\pip install pyinstaller
winget install JRSoftware.InnoSetup
.venv\Scripts\python packaging\build.py
```

The installer lands in `dist\`. Pushing a `v*` tag builds it on GitHub and attaches it to a release.

## Add a command

```python
@command("say", "Speak text on the PC.", tab="tools", icon="volume")
def say(text: str = ""):
    ...
```

This becomes `GET/POST /say?text=…` and a button in the app.
