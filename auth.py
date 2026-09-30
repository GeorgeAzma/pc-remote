"""Sign-in: on by default, and can be switched off in Controls → About.

A device signs in once, from anywhere (home Wi-Fi, Tailscale), with the
access code or your own password, or by scanning the pairing QR code shown
on the PC or on any device that's already signed in. Signed-in devices
hold a key and stay signed in until you sign them all out (a new key).
The PC itself (127.0.0.1) never needs to sign in.

Stored in %LOCALAPPDATA%\\PC Remote\\auth.json (or the source folder when
run from source): the key, the generated code (so the PC can show it), or
a scrypt hash when you pick your own password."""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

import paths

FILE = os.path.join(paths.DATA, "auth.json")
_ALPHA = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I: easy to read and type
_lock = threading.Lock()
_cfg: dict | None = None
_fails: dict = {}  # ip -> (failures, locked until)


def _new_code() -> str:
    s = "".join(secrets.choice(_ALPHA) for _ in range(8))
    return f"{s[:4]}-{s[4:]}"


def _norm(code: str) -> str:
    return "".join(c for c in code.upper() if c.isalnum())


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def _save(cfg: dict):
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=1)
    os.replace(tmp, FILE)


def _load() -> dict:  # under _lock
    global _cfg
    if _cfg is None:
        try:
            with open(FILE, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            cfg = {}
        fresh = dict(cfg)
        cfg.setdefault("enabled", True)
        cfg.setdefault("key", secrets.token_hex(16))
        if not cfg.get("hash") and not cfg.get("code"):
            cfg["code"] = _new_code()
        if cfg != fresh:
            _save(cfg)
        _cfg = cfg
    return _cfg


def required() -> bool:
    with _lock:
        return bool(_load()["enabled"])


def key() -> str:
    with _lock:
        return _load()["key"]


def valid(candidate: str) -> bool:
    if not candidate:
        return False
    return hmac.compare_digest(candidate.encode(), key().encode())


def _check(password: str) -> bool:  # under _lock
    cfg = _load()
    if cfg.get("hash"):
        return hmac.compare_digest(_hash(password, bytes.fromhex(cfg["salt"])), bytes.fromhex(cfg["hash"]))
    return hmac.compare_digest(_norm(password).encode(), _norm(cfg.get("code", "")).encode())


def login(ip: str, password: str):
    """-> (key, None) when right, (None, seconds to wait) when not. Five
    wrong tries lock that address out for 30 s, doubling after that."""
    now = time.monotonic()
    fails, until = _fails.get(ip, (0, 0.0))
    if now < until:
        return None, int(until - now) + 1
    with _lock:
        ok = _check(password or "")
        k = _load()["key"]
    if ok:
        _fails.pop(ip, None)
        return k, None
    fails += 1
    wait = 30 * 2 ** (fails - 5) if fails >= 5 else 0
    _fails[ip] = (fails, now + min(wait, 900))
    return None, wait or None


def status(include_secrets: bool) -> dict:
    with _lock:
        cfg = _load()
        out = {"enabled": cfg["enabled"], "own_password": bool(cfg.get("hash"))}
        if include_secrets:
            out["code"] = cfg.get("code")
            out["key"] = cfg["key"]
        return out


def update(enabled=None, password=None, new_code=False, new_key=False) -> dict:
    """Settings: switch sign-in on/off, set your own password (or go back to
    a generated code), or sign every device out (a new key)."""
    with _lock:
        cfg = _load()
        if enabled is not None:
            cfg["enabled"] = bool(enabled)
        if password is not None:
            if len(password) < 6:
                raise ValueError("use at least 6 characters")
            salt = secrets.token_bytes(16)
            cfg.update(salt=salt.hex(), hash=_hash(password, salt).hex())
            cfg.pop("code", None)
        if new_code:
            cfg["code"] = _new_code()
            cfg.pop("hash", None)
            cfg.pop("salt", None)
        if new_key:
            cfg["key"] = secrets.token_hex(16)
        _save(cfg)
    return status(True)
