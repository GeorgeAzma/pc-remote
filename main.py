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
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import auth
import certs
import commands
import paths
import remote
import terminal
import video
import wsock

HOST = os.environ.get("PC_API_HOST", "0.0.0.0")
PORT = int(os.environ.get("PC_API_PORT", "1024"))
TOKEN = os.environ.get("PC_API_TOKEN", "")
WEB = os.path.join(paths.RES, "web")
HOME = os.path.realpath(os.path.expanduser("~"))
DOWNLOADS = os.path.join(HOME, "Downloads")
# programs that connect from this PC on behalf of something else: containers,
# WSL, tunnels, port forwards. They sign in like any other device.
FORWARDERS = {"com.docker.backend", "com.docker.proxy", "vpnkit", "wslrelay", "wslhost", "tailscaled", "tailscale",
              "ssh", "sshd", "cloudflared", "ngrok", "frpc", "svchost"}

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

    def _local(self) -> bool:
        """The PC itself: never asked to sign in (it's where you'd see the
        code), whichever of its addresses it opens. Not a proxy that forwards
        someone else (X-Forwarded-For)."""
        if self.headers.get("X-Forwarded-For") or self.headers.get("Forwarded"):
            return False
        if self._pc is None:  # (per connection: it can't change)
            self._pc = self._from_pc()
        return self._pc

    _pc = None

    def _from_pc(self) -> bool:
        """A program in your session on this PC, not something that relays for
        others. A connection to one of the PC's own addresses comes from that
        same address, but so do WSL and containers (behind the PC's address or
        through Docker on 127.0.0.1): what tells them apart is who owns the
        other end of the connection."""
        try:
            peer_ip, peer_port = self.client_address[:2]
            own_ip, own_port = self.connection.getsockname()[:2]
            peer, own = ipaddress.ip_address(peer_ip), ipaddress.ip_address(own_ip)
        except (OSError, ValueError):
            return False
        if not (peer.is_loopback or peer == own):
            return False  # another device
        pid = win32.tcp_owner(str(peer), peer_port, str(own), own_port)
        if not pid:
            return False  # no program on the PC: WSL, a VM
        name = os.path.splitext(os.path.basename(win32.process_path(pid) or ""))[0].lower()
        return (bool(name) and name not in FORWARDERS
                and win32.process_session(pid) == win32.process_session(os.getpid()))

    def _credential(self, query) -> str:
        return query.get("token", [""])[0] or self.headers.get("X-Token", "") or self._cookie_token()

    def _signed_in(self, query) -> bool:
        cand = self._credential(query)
        return auth.valid(cand) or bool(TOKEN) and hmac.compare_digest(cand.encode(), TOKEN.encode())

    def _authorized(self, query, reply=True) -> bool:
        if not (auth.required() or TOKEN) or self._signed_in(query) or self._local():
            return True
        if reply:
            self.close_connection = True  # the request body (if any) goes unread
            self._json(401, {"error": "sign in required"})
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
        if route == "/api/auth":  # public: does this device need to sign in?
            return self._json(200, {"required": not self._authorized(query, reply=False)})
        if not self._authorized(query):
            return
        if route == "/api/security":
            return self._json(200, self._security())
        if route in ("/ws", "/vstream", "/term", "/sysmon/live"):
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
        if route == "/appicon":
            try:
                png = commands.app_icon(query.get("app", [""])[0], int(query.get("pid", ["0"])[0] or 0),
                                        query.get("name", [""])[0])
            except (ValueError, OSError):
                png = None
            if not png:
                return self._json(404, {"error": "no icon"})
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.send_header("Cache-Control", "private, max-age=86400")
            self.end_headers()
            self.wfile.write(png)
            return
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
        if not self._guard():
            return
        if route == "/api/login":  # public, rate-limited per address
            body = self._body(limit=4096)
            if body is None:
                return
            password = body.get("password") if isinstance(body, dict) else None
            key, wait = auth.login(self.client_address[0], password if isinstance(password, str) else "")
            if key:
                return self._json(200, {"key": key})
            return self._json(429 if wait else 403, {"error": f"too many tries, wait {wait} s" if wait else "wrong code or password",
                                                     "wait": wait})
        if not self._authorized(query):
            return
        if route == "/upload":
            return self._upload(query)
        body = self._body()
        if body is None:
            return
        if not isinstance(body, dict):
            body = {}
        if route == "/api/security":
            try:
                auth.update(enabled=body.get("enabled"), password=body.get("password"),
                            new_code=bool(body.get("new_code")), new_key=bool(body.get("new_key")))
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            return self._json(200, self._security())
        args = {k: v[0] for k, v in query.items()}
        args.update(body)
        return self._command(route.lstrip("/"), args)

    def _body(self, limit=None):
        """The JSON request body ({} if none), or None after replying with an error."""
        length = int(self.headers.get("Content-Length") or 0)
        if limit is not None and length > limit:
            self.close_connection = True
            self._json(413, {"error": "request too large"})
            return None
        try:
            return json.loads(self.rfile.read(length) or b"{}") if length else {}
        except ValueError:
            self._json(400, {"error": "invalid JSON body"})
            return None

    def _index(self, query):
        headers = []
        if self._signed_in(query):  # e.g. a pairing link: remember it as a cookie too
            headers.append(("Set-Cookie", f"pct={self._credential(query)}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict"))
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

    def _security(self):
        """Sign-in settings, the code and the pairing key (only ever sent to
        the PC itself or a device that's signed in), and the addresses a new
        device could use."""
        return {**auth.status(include_secrets=True), "port": PORT, "env_token": bool(TOKEN),
                "ips": [ip for ip in certs.local_ips() if ip != "127.0.0.1"]}

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
            elif route == "/sysmon/live":
                import sysmon
                sysmon.MONITOR.serve(ws)
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


def _running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
            return True
    except OSError:
        return False


def _open_page():
    import webbrowser
    webbrowser.open(f"http://localhost:{PORT}/?welcome")  # the PC's addresses, to open on a phone


def main():
    """PC Remote.exe            start (or find) the server and open its page
       PC Remote.exe --background   just serve (the sign-in task)
       PC Remote.exe --setup / --unsetup   startup task + firewall (the installer)"""
    args = sys.argv[1:]
    if "--setup" in args or "--unsetup" in args:
        import setup_win
        if "--setup" in args:
            setup_win.install(sys.executable, startup="--no-startup" not in args)
        else:
            setup_win.uninstall()
        return
    background = "--background" in args
    if _running():  # already serving: someone opened it from the Start menu
        if not background:
            _open_page()
        return
    if paths.FROZEN and not background:
        import setup_win
        if setup_win.task_exists() and setup_win.run_task():  # the elevated copy, no UAC prompt
            for _ in range(60):
                if _running():
                    break
                time.sleep(0.25)
            _open_page()
            return
    if sys.stderr is None:  # no console: keep a small log instead
        logging.basicConfig(filename=os.path.join(paths.DATA, "server.log"), level=logging.WARNING,
                            format="%(asctime)s %(levelname)s %(message)s")
    else:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    # a crash in a worker thread would otherwise vanish without a console
    threading.excepthook = lambda a: log.error("thread %s crashed", a.thread and a.thread.name,
                                               exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    server = Server((HOST, PORT), Handler)
    if not background and paths.FROZEN:
        threading.Timer(0.8, _open_page).start()
    if not os.environ.get("PC_NO_TLS"):
        try:
            server.tls = certs.context()
        except (OSError, ssl.SSLError, ValueError) as exc:
            log.warning("HTTPS disabled: %s", exc)
    ips = [ip for ip in certs.local_ips() if ip != "127.0.0.1"] or ["localhost"]
    log.info("PC Remote on http://%s:%d/%s", ips[0], PORT, "  (HTTPS on the same port)" if server.tls else "")
    if not video.FFMPEG:
        log.warning("ffmpeg not found - screen streaming disabled (winget install Gyan.FFmpeg)")
    if not TOKEN and not auth.required():
        log.warning("sign-in is off - anyone who can reach this PC can control it")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
