#!/usr/bin/env python3
"""
PC Remote
=========
Control this Windows PC from a phone on the same network: live low-latency
screen + trackpad + keyboard, a real terminal, and one-tap power/media
controls. Every action is a plain function decorated with @command (see
commands.py), exposed both in the web UI and as an HTTP endpoint.

    .venv\\Scripts\\pythonw.exe main.py          (or launch_remote.bat)
    open  http://<PC-IP>:1024/   (HD stream: https://<PC-IP>:1024/)

HTTP and HTTPS share the port: the first byte of each connection tells a
TLS ClientHello (0x16) apart from a plain request.

Environment:
    PC_API_HOST   bind address          (default 0.0.0.0)
    PC_API_PORT   port                  (default 1024)
    PC_API_TOKEN  require ?token=...    (default: open to the LAN)
    PC_FFMPEG     path to ffmpeg.exe    (default: auto-detect)
    PC_NO_TLS=1   disable HTTPS
"""
import win32  # noqa: I001 - first: sets per-monitor DPI awareness before any metrics call

import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import socket
import ssl
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import certs
import commands
import remote
import terminal
import video
import wsock

HOST = os.environ.get("PC_API_HOST", "0.0.0.0")
PORT = int(os.environ.get("PC_API_PORT", "1024"))
TOKEN = os.environ.get("PC_API_TOKEN", "")
ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, "web")
HOME = os.path.realpath(os.path.expanduser("~"))
DOWNLOADS = os.path.join(HOME, "Downloads")

ALLOWED_HOSTS = {h.strip().lower() for h in os.environ.get("PC_ALLOWED_HOSTS", "").split(",") if h.strip()}

log = logging.getLogger("pc-remote")


def _host_allowed(host_header: str) -> bool:
    """IP literals, localhost and this PC's own name (incl. e.g. its
    Tailscale MagicDNS name 'pc.tailnet.ts.net') - not arbitrary domains,
    which is what a DNS-rebinding attack would present."""
    h = host_header.strip().lower()
    if h.startswith("["):        # [ipv6]:port
        h = h[1:h.find("]")]
    elif h.count(":") == 1:      # name:port or ipv4:port
        h = h.split(":")[0]
    h = h.rstrip(".")
    if not h:
        return True
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    me = socket.gethostname().lower()
    return h in ("localhost", me) or h.endswith(".localhost") or h.startswith(me + ".") or h in ALLOWED_HOSTS
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("application/manifest+json", ".webmanifest")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # SO_REUSEADDR on Windows lets a second process hijack a bound port;
    # exclusive use makes a duplicate launch fail loudly instead.
    allow_reuse_address = False
    tls: ssl.SSLContext | None = None

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def finish_request(self, request, client_address):
        if self.tls:
            try:
                request.settimeout(10)
                first = request.recv(1, socket.MSG_PEEK)
                if first == b"\x16":
                    request = self.tls.wrap_socket(request, server_side=True)
                request.settimeout(None)
            except (OSError, ssl.SSLError):
                return
        try:
            self.RequestHandlerClass(request, client_address, self)
        finally:
            if isinstance(request, ssl.SSLSocket):
                try:
                    request.close()
                except OSError:
                    pass

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError, ssl.SSLError, OSError)):
            return  # phones drop connections all the time
        log.exception("request from %s failed", client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "PCRemote/2"
    protocol_version = "HTTP/1.1"
    timeout = 120

    def log_message(self, *args):
        pass

    # --- helpers --------------------------------------------------------
    def _json(self, code: int, payload, extra_headers=()):
        body = json.dumps(payload, separators=(",", ":"), default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra_headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _cookie_token(self) -> str:
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "pct":
                return v
        return ""

    def _authorized(self, query) -> bool:
        if not TOKEN:
            return True
        cand = query.get("token", [""])[0] or self.headers.get("X-Token", "") or self._cookie_token()
        if hmac.compare_digest(cand.encode(), TOKEN.encode()):
            return True
        self._json(401, {"error": "unauthorized", "hint": "open /?token=YOUR_TOKEN"})
        return False

    def _parse(self):
        u = urlparse(self.path)
        return (u.path.rstrip("/") or "/"), parse_qs(u.query)

    def _guard(self, allow_cross_site=False) -> bool:
        """Refuse requests a *web page* makes through your browser. Without
        this, any site you visit could fetch http://127.0.0.1:1024/shutdown
        or open ws://127.0.0.1:1024/term. Browsers tag such requests
        (Sec-Fetch-Site, Origin); curl and scripts send neither."""
        if not _host_allowed(self.headers.get("Host", "")):
            # DNS rebinding: evil.example resolving to this PC.
            self._json(403, {"error": "unknown host name",
                             "hint": "add it to PC_ALLOWED_HOSTS (comma-separated)"})
            return False
        if allow_cross_site:
            return True
        site = self.headers.get("Sec-Fetch-Site")
        origin = self.headers.get("Origin")
        cross = bool(site) and site not in ("same-origin", "none")
        if origin and not cross:
            cross = origin == "null" or urlparse(origin).netloc.lower() != self.headers.get("Host", "").lower()
        if cross:
            self._json(403, {"error": "cross-site request refused"})
            return False
        return True

    def _file(self, path: str, content_type: str | None = None, attachment: str | None = None,
              cache: str = "no-cache"):
        try:
            st = os.stat(path)
        except OSError:
            return self._json(404, {"error": "not found"})
        etag = f'"{int(st.st_mtime)}-{st.st_size}"'
        if not attachment and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type or mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(st.st_size))
        self.send_header("Cache-Control", cache)
        self.send_header("ETag", etag)
        if attachment:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(attachment)}")
        self.end_headers()
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                self.wfile.write(chunk)

    # --- routing --------------------------------------------------------
    def do_GET(self):
        route, query = self._parse()
        commands.record()
        public = route == "/" or route.startswith("/static/") or route in ("/manifest.webmanifest", "/ca.crt")
        if not self._guard(allow_cross_site=public):  # following a link to the app is fine
            return
        if route == "/":
            return self._index(query)
        if route.startswith("/static/"):
            name = os.path.basename(route[len("/static/"):])
            return self._file(os.path.join(WEB, name))
        if route == "/manifest.webmanifest":
            return self._file(os.path.join(WEB, "manifest.webmanifest"))
        if route == "/ca.crt":
            certs.ensure()
            return self._file(certs.CA_CERT, "application/x-x509-ca-cert", attachment="pc-remote-ca.crt")
        if not self._authorized(query):
            return
        if route in ("/ws", "/vstream", "/term"):
            return self._websocket(route, query)
        if route == "/api/info":
            return self._json(200, self._info())
        if route == "/api/commands":
            return self._json(200, commands.describe())
        if route == "/api/state":
            names = [n for n in query.get("names", [""])[0].split(",") if n]
            return self._json(200, {"state": commands.states(names or None), "pending": commands.pending_info()})
        if route == "/api/files":
            return self._list_files(query.get("path", [""])[0])
        if route == "/download":
            return self._download(query.get("path", [""])[0])
        if route == "/screenshot.png":
            data = win32.screenshot_png()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        return self._command(route.lstrip("/"), {k: v[0] for k, v in query.items()})

    def do_POST(self):
        route, query = self._parse()
        commands.record()
        if not self._guard() or not self._authorized(query):
            return
        if route == "/upload":
            return self._upload(query)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except ValueError:
            return self._json(400, {"error": "invalid JSON body"})
        args = {k: v[0] for k, v in query.items()}
        if isinstance(body, dict):
            args.update(body)
        return self._command(route.lstrip("/"), args)

    def _index(self, query):
        if not self._authorized(query):
            return
        headers = []
        if TOKEN:
            headers.append(("Set-Cookie", f"pct={TOKEN}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict"))
        path = os.path.join(WEB, "index.html")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _info(self):
        host = self.headers.get("Host", f"localhost:{PORT}").split(":")[0]
        return {"host": socket.gethostname(), "secure": isinstance(self.connection, ssl.SSLSocket),
                "https": f"https://{host}:{PORT}/" if self.server.tls else None,
                "token": bool(TOKEN), "video": video.status(), "displays": win32.displays(),
                "shells": [{k: s[k] for k in ("id", "name")} for s in terminal.available_shells()],
                "pending": commands.pending_info(), "time": time.time()}

    def _command(self, name: str, args: dict):
        entry = commands.commands.get(name.lower())
        if not entry:
            return self._json(404, {"error": f"unknown command: {name}", "available": list(commands.commands)})
        try:
            result = commands.run(name.lower(), args)
            commands.record(name)
        except Exception as exc:  # noqa: BLE001 - report any command failure to the UI
            return self._json(500, {"command": name, "error": str(exc) or type(exc).__name__})
        return self._json(200, {"command": name, "result": result})

    def _websocket(self, route, query):
        ws = wsock.WebSocket.accept(self, sndbuf=512 * 1024 if route == "/vstream" else 0)
        if not ws:
            return
        # Clients heartbeat every 15s; a socket silent for a minute is a
        # phone that vanished without a FIN - free its thread and encoder.
        self.connection.settimeout(60)
        try:
            if route == "/vstream":
                video.StreamSession(ws).run()
            elif route == "/ws":
                remote.InputSession(ws).run()
            else:
                terminal.serve(ws, query)
        finally:
            ws.close()

    # --- files ------------------------------------------------------------
    def _upload(self, query):
        """Raw-body upload (the UI streams each file with XHR so it can show
        progress); written straight to disk, never buffered in memory."""
        name = os.path.basename(query.get("name", ["upload"])[0].replace("\\", "/")) or "upload"
        length = int(self.headers.get("Content-Length") or 0)
        os.makedirs(DOWNLOADS, exist_ok=True)
        base, ext = os.path.splitext(name)
        dest, n = os.path.join(DOWNLOADS, name), 1
        while os.path.exists(dest):
            dest, n = os.path.join(DOWNLOADS, f"{base} ({n}){ext}"), n + 1
        tmp = dest + ".part"
        remaining = length
        try:
            with open(tmp, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        raise ConnectionError("upload interrupted")
                    f.write(chunk)
                    remaining -= len(chunk)
            os.replace(tmp, dest)
        except (OSError, ConnectionError) as exc:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return self._json(500, {"error": str(exc)})
        return self._json(200, {"status": "saved", "file": os.path.basename(dest), "folder": DOWNLOADS})

    def _safe_path(self, path: str) -> str | None:
        p = os.path.realpath(path or DOWNLOADS)
        try:
            return p if os.path.commonpath([p, HOME]) == HOME else None
        except ValueError:  # on a different drive
            return None

    def _list_files(self, path):
        p = self._safe_path(path)
        if not p or not os.path.isdir(p):
            return self._json(404, {"error": "folder not found"})
        entries = []
        with os.scandir(p) as it:
            for e in it:
                if e.name.startswith((".", "$")) or e.name.lower() in ("desktop.ini", "ntuser.dat"):
                    continue
                try:
                    st = e.stat()
                    entries.append({"name": e.name, "dir": e.is_dir(), "size": st.st_size, "mtime": st.st_mtime})
                except OSError:
                    continue
        entries.sort(key=lambda e: (not e["dir"], -e["mtime"]))
        places = [{"name": n, "path": os.path.join(HOME, n)} for n in
                  ("Downloads", "Desktop", "Documents", "Pictures", "Videos", "Music") if os.path.isdir(os.path.join(HOME, n))]
        return self._json(200, {"path": p, "parent": os.path.dirname(p) if p != HOME else None,
                                "entries": entries[:500], "places": places, "home": HOME})

    def _download(self, path):
        p = self._safe_path(path)
        if not p or not os.path.isfile(p):
            return self._json(404, {"error": "file not found"})
        return self._file(p, attachment=os.path.basename(p), cache="no-store")


def main():
    if sys.stderr is None:  # pythonw: no console, keep a small log instead
        logging.basicConfig(filename=os.path.join(ROOT, "server.log"), level=logging.WARNING,
                            format="%(asctime)s %(levelname)s %(message)s")
    else:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    server = Server((HOST, PORT), Handler)
    if not os.environ.get("PC_NO_TLS"):
        try:
            server.tls = certs.context()
        except (OSError, ssl.SSLError, ValueError) as exc:
            log.warning("HTTPS disabled: %s", exc)
    ips = [ip for ip in certs.local_ips() if ip != "127.0.0.1"] or ["localhost"]
    log.info("PC Remote on http://%s:%d/%s", ips[0], PORT, "  (HTTPS on the same port)" if server.tls else "")
    if not video.FFMPEG:
        log.warning("ffmpeg not found - screen streaming disabled (winget install Gyan.FFmpeg)")
    if not TOKEN:
        log.warning("no PC_API_TOKEN set - anyone on your network can control this PC")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
