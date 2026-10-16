#!/usr/bin/env python3
"""Hermes Web Chat — satu halaman chat khusus untuk Hermes Agent.

Kenapa ada: `hermes dashboard` hanya meng-embed TUI asli ke xterm.js (jadi terasa
seperti terminal), dan frontend generik (Open WebUI dll) mengabaikan event khusus
Hermes. Server kecil ini menyajikan satu halaman HTML dan mem-proxy request ke
API server Hermes yang OpenAI-compatible di 127.0.0.1:8642.

Poin penting: API key TIDAK pernah sampai ke browser. Browser bicara ke server ini
(same-origin), server ini yang menempelkan `Authorization: Bearer <API_SERVER_KEY>`.

stdlib saja — tidak butuh pip/aiohttp. Dijalankan oleh hermes-webchat.service.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import os
import posixpath
import secrets
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
UPSTREAM = os.environ.get("HERMES_API_URL", "http://127.0.0.1:8642")
LISTEN_HOST = os.environ.get("WEBCHAT_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("WEBCHAT_PORT", "8650"))
UPSTREAM_TIMEOUT = float(os.environ.get("WEBCHAT_UPSTREAM_TIMEOUT", "1800"))

_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def read_api_key() -> str:
    """API_SERVER_KEY dari .env — dibaca sekali saat start, boleh di-override lewat env."""
    key = (os.environ.get("API_SERVER_KEY") or "").strip()
    if key:
        return key
    env_file = HERMES_HOME / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("API_SERVER_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


API_KEY = read_api_key()

# ---------------------------------------------------------------------------
# Autentikasi
#
# Webchat ini menempelkan API_SERVER_KEY milik gateway Hermes ke tiap request,
# jadi siapa pun yang bisa membuka port 8650 praktis memakai agent sebagai root.
# Karena itu semua route (kecuali halaman login + healthz) butuh sesi login.
#
# Penyimpanan: satu file auth.json (mode 600) berisi username, hash PBKDF2-SHA256,
# dan `secret` untuk menandatangani cookie sesi. Cookie-nya stateless (HMAC), jadi
# sesi tetap hidup setelah service restart; ganti password tidak menghapus sesi,
# tapi menghapus/mengganti `secret` akan (turunkan secret untuk memaksa logout semua).
# ---------------------------------------------------------------------------

AUTH_FILE = Path(os.environ.get("WEBCHAT_AUTH_FILE") or (HERE / "auth.json"))
COOKIE_NAME = os.environ.get("WEBCHAT_COOKIE", "hermes_chat")
SESSION_TTL = int(os.environ.get("WEBCHAT_SESSION_TTL", str(7 * 24 * 3600)))  # 7 hari
PBKDF2_ROUNDS = int(os.environ.get("WEBCHAT_PBKDF2_ROUNDS", "210000"))
MAX_FAILS = int(os.environ.get("WEBCHAT_MAX_FAILS", "5"))
LOCK_SECONDS = int(os.environ.get("WEBCHAT_LOCK_SECONDS", "900"))  # 15 menit

_auth_lock = threading.Lock()
_auth_cache: dict = {"mtime": None, "data": None}
_fails_lock = threading.Lock()
_fails: dict = {}  # ip -> [timestamp gagal login]
_revoked_lock = threading.Lock()
_revoked: dict = {}  # nonce sesi yang dicabut (logout) -> exp token

# Route yang boleh diakses tanpa login. Semua sisanya wajib sesi.
PUBLIC_PATHS = {"/login", "/healthz", "/favicon.ico"}


def _pbkdf2(password: str, salt: bytes, rounds: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)


def write_auth(username: str, password: str) -> Path:
    """Tulis auth.json (mode 600) berisi hash password baru. Secret dipertahankan
    kalau sudah ada, supaya mengganti password tidak memaksa semua sesi login ulang."""
    previous = load_auth() or {}
    salt = secrets.token_bytes(16)
    rounds = PBKDF2_ROUNDS
    data = {
        "username": username,
        "salt": salt.hex(),
        "hash": _pbkdf2(password, salt, rounds).hex(),
        "iterations": rounds,
        "secret": previous.get("secret") or secrets.token_hex(32),
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(AUTH_FILE, 0o600)
    with _auth_lock:
        _auth_cache["mtime"] = None
        _auth_cache["data"] = None
    return AUTH_FILE


def load_auth():
    """Baca auth.json, di-cache berdasarkan mtime — jadi `--set-password` langsung
    berlaku tanpa restart service."""
    try:
        mtime = AUTH_FILE.stat().st_mtime
    except OSError:
        return None
    with _auth_lock:
        if _auth_cache["mtime"] != mtime:
            try:
                _auth_cache["data"] = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                _auth_cache["data"] = None
            _auth_cache["mtime"] = mtime
        return _auth_cache["data"]


def verify_credentials(username: str, password: str) -> bool:
    data = load_auth()
    if not data:
        return False
    try:
        salt = bytes.fromhex(data.get("salt") or "")
        rounds = int(data.get("iterations") or PBKDF2_ROUNDS)
    except ValueError:
        return False
    expected = str(data.get("hash") or "")
    got = _pbkdf2(password or "", salt, rounds).hex()
    # Dua perbandingan dijalankan penuh (tanpa short-circuit) — waktu respons tidak
    # membocorkan apakah username yang salah atau password.
    user_ok = hmac.compare_digest(str(data.get("username") or ""), username or "")
    pass_ok = hmac.compare_digest(expected, got)
    return bool(user_ok and pass_ok)


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_session(username: str) -> str:
    data = load_auth() or {}
    payload = "%s|%d|%s" % (username, int(time.time()) + SESSION_TTL, secrets.token_hex(8))
    raw = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    return raw + "." + _sign(str(data.get("secret") or ""), payload)


def session_claims(token: str):
    """(user, nonce, exp) kalau token sah, dicabut belum, dan belum kedaluwarsa."""
    if not token or "." not in token:
        return None
    raw, _, sig = token.rpartition(".")
    data = load_auth() or {}
    try:
        payload = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
    except Exception:  # noqa: BLE001 - token rusak dari klien
        return None
    if not hmac.compare_digest(_sign(str(data.get("secret") or ""), payload), sig):
        return None
    parts = payload.split("|")
    if len(parts) != 3:
        return None
    user, exp_raw, nonce = parts
    try:
        exp = int(exp_raw)
    except ValueError:
        return None
    if exp < time.time() or is_revoked(nonce):
        return None
    return user, nonce, exp


def session_user(token: str) -> str:
    claims = session_claims(token)
    return claims[0] if claims else ""


def revoke_nonce(nonce: str, exp: int) -> None:
    """Cabut satu sesi (dipakai logout). Stateless token tidak bisa ditarik
    dari sisi klien, jadi nonce-nya dicatat sampai token itu kedaluwarsa sendiri —
    tanpa mematikan sesi di perangkat lain."""
    now = time.time()
    with _revoked_lock:
        for key in [k for k, e in _revoked.items() if e < now]:
            _revoked.pop(key, None)
        if len(_revoked) < 10000:
            _revoked[nonce] = exp


def is_revoked(nonce: str) -> bool:
    now = time.time()
    with _revoked_lock:
        exp = _revoked.get(nonce)
        if exp is None:
            return False
        if exp < now:
            _revoked.pop(nonce, None)
            return False
        return True


def _prune_fails(ip: str) -> int:
    """Jumlah kegagalan login dalam jendela waktu; sekaligus buang yang kedaluwarsa."""
    now = time.time()
    with _fails_lock:
        marks = [t for t in _fails.get(ip, []) if now - t < LOCK_SECONDS]
        if marks:
            _fails[ip] = marks
        else:
            _fails.pop(ip, None)
        return len(marks)


def record_fail(ip: str) -> None:
    with _fails_lock:
        _fails.setdefault(ip, []).append(time.time())


def clear_fails(ip: str) -> None:
    with _fails_lock:
        _fails.pop(ip, None)


def _server_start_banner() -> None:
    data = load_auth()
    if data:
        sys.stderr.write("[webchat] login aktif untuk user %r (%s)\n" % (data.get("username"), AUTH_FILE))
        return
    password = secrets.token_urlsafe(12)
    write_auth("admin", password)
    sys.stderr.write(
        "[webchat] auth.json belum ada — dibuatkan otomatis.\n"
        "[webchat]   user     : admin\n"
        "[webchat]   password : %s\n"
        "[webchat]   ganti dengan: python3 %s --set-password\n" % (password, Path(__file__).name)
    )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "hermes-webchat/1.0"

    # -- helpers ---------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature
        sys.stderr.write("[webchat] %s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, status: int, payload: dict, extra_headers: list | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or []):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, status: int, message: str, detail: str = "",
              extra_headers: list | None = None) -> None:
        self._json(status, {"error": message, "detail": detail}, extra_headers)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    # -- auth helpers ----------------------------------------------------

    def _cookie(self, name: str) -> str:
        raw = self.headers.get("Cookie")
        if not raw:
            return ""
        try:
            jar = SimpleCookie()
            jar.load(raw)
        except Exception:  # noqa: BLE001 - header Cookie rusak dari klien
            return ""
        morsel = jar.get(name)
        return morsel.value if morsel else ""

    def _token(self) -> str:
        """Cookie sesi, atau `Authorization: Bearer <token>` untuk klien/curl."""
        auth = (self.headers.get("Authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return self._cookie(COOKIE_NAME)

    def _user(self) -> str:
        return session_user(self._token())

    def _same_origin(self) -> bool:
        """Cek ringan anti-CSRF: kalau browser mengirim Origin/Referer, host-nya
        harus sama dengan host request ini. Cookie sesi juga SameSite=Lax."""
        host = self.headers.get("Host") or ""
        for header in ("Origin", "Referer"):
            value = self.headers.get(header)
            if not value:
                continue
            if urllib.parse.urlsplit(value).netloc != host:
                return False
        return True

    def _require_auth(self) -> str:
        """Kembalikan username kalau sudah login; kalau tidak, kirim respons
        (401 JSON untuk panggilan API, 302 ke /login untuk navigasi halaman)
        lalu kembalikan string kosong."""
        user = self._user()
        if user:
            return user
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/") or path.startswith("/v1/"):
            self._fail(401, "login required", "Buka /login lalu masukkan kredensial.")
        else:
            nxt = urllib.parse.quote(self.path if self.path != "/" else "/", safe="/?=&")
            self._redirect("/login?next=" + nxt)
        return ""

    # -- routes ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        path = urllib.parse.urlsplit(self.path).path
        if path == "/healthz":
            # Publik tapi minimal kalau belum login — cukup untuk cek proses hidup.
            user = self._user()
            if user:
                return self._json(200, {"status": "ok", "upstream": UPSTREAM, "auth": "session",
                                        "user": user, "key_loaded": bool(API_KEY)})
            return self._json(200, {"status": "ok", "auth": "none"})
        if path == "/login":
            if self._user():
                return self._redirect(self._next_target())
            return self._serve_static("login.html")
        if path == "/logout":
            return self._redirect("/login")
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not self._require_auth():
            return
        if path in ("/", "/index.html"):
            return self._serve_static("index.html")
        if path == "/api/models":
            return self._proxy_models()
        # Hermes-specific read-only surface (curated model inventory + session history),
        # proxied so the browser can reach it same-origin without ever holding the key.
        if path == "/api/model/options" or path == "/api/sessions" or path.startswith("/api/sessions/"):
            return self._proxy_get()
        # Status run: dipakai UI untuk rekonsiliasi setelah reload.
        if path.startswith("/v1/runs/") and path.endswith("/events"):
            return self._proxy_stream_get()          # SSE, dialirkan apa adanya
        if (path == "/v1/runs" or path.startswith("/v1/runs/")):
            return self._proxy_get()
        self._fail(404, "not found", path)

    def _next_target(self) -> str:
        """Tujuan setelah login — hanya path lokal (tanpa skema/host) supaya
        parameter `next` tidak bisa dipakai untuk open redirect."""
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        candidate = (query.get("next") or [""])[0].strip()
        if not candidate.startswith("/") or candidate.startswith("//"):
            return "/"
        return candidate

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        path = urllib.parse.urlsplit(self.path).path
        if path == "/login":
            return self._do_login()
        if path == "/logout":
            return self._do_logout()
        if not self._require_auth():
            return
        if not self._same_origin():
            return self._fail(403, "cross-origin ditolak")
        if path == "/v1/chat/completions":
            return self._proxy_chat()
        # Kontrol run: buat run, jawab approval, stop, steer. Semuanya JSON pendek,
        # bukan stream. Inilah yang membuat kartu konfirmasi di UI bisa dijawab.
        if path == "/v1/runs" or path.startswith("/v1/runs/"):
            return self._proxy_post_json()
        self._fail(404, "not found", path)

    # -- login / logout --------------------------------------------------

    def _read_body(self, limit: int = 64 * 1024) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        if length <= 0 or length > limit:
            return b""
        return self.rfile.read(length)

    def _do_login(self) -> None:
        ip = self.client_address[0]
        if not self._same_origin():
            return self._fail(403, "cross-origin ditolak")
        if load_auth() is None:
            return self._fail(503, "belum ada kredensial",
                              "auth.json tidak ada — jalankan: python3 server.py --set-password")
        if _prune_fails(ip) >= MAX_FAILS:
            return self._fail(429, "terlalu banyak percobaan",
                              "Tunggu %d menit sebelum mencoba lagi." % (LOCK_SECONDS // 60))

        body = self._read_body()
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        username = password = ""
        try:
            if ctype == "application/json":
                payload = json.loads(body.decode("utf-8") or "{}")
                username = str(payload.get("username") or "")
                password = str(payload.get("password") or "")
            else:
                fields = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
                username = (fields.get("username") or [""])[0]
                password = (fields.get("password") or [""])[0]
        except (ValueError, UnicodeDecodeError):
            record_fail(ip)
            return self._fail(400, "body login tidak valid")

        if not verify_credentials(username, password):
            record_fail(ip)
            left = max(0, MAX_FAILS - _prune_fails(ip))
            time.sleep(0.4)  # perlambat brute force
            return self._fail(401, "username atau password salah",
                              "Sisa %d percobaan sebelum terkunci." % left)

        clear_fails(ip)
        cookie = "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (
            COOKIE_NAME, issue_session(username), SESSION_TTL)
        self._json(200, {"ok": True, "username": username, "redirect": self._next_target()},
                   extra_headers=[("Set-Cookie", cookie)])

    def _do_logout(self) -> None:
        claims = session_claims(self._token())
        if claims:  # token sesi dicabut server-side, bukan hanya dibuang browser
            revoke_nonce(claims[1], claims[2])
        expired = "%s=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0" % COOKIE_NAME
        if (self.headers.get("Accept") or "").find("text/html") >= 0:
            return self._redirect("/login")
        self._json(200, {"ok": True}, extra_headers=[("Set-Cookie", expired)])

    def _proxy_post_json(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._fail(400, "bad Content-Length")
        if length < 0 or length > 1024 * 1024:
            return self._fail(400, "bad request body size")
        body = self.rfile.read(length) if length else b""
        req = urllib.request.Request(
            UPSTREAM + self.path, data=body, method="POST",
            headers=self._upstream_headers(stream=False),
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            return self._upstream_error(exc)
        except Exception as exc:  # noqa: BLE001 - dilaporkan ke UI
            return self._upstream_error(exc)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    # -- static ----------------------------------------------------------

    def _serve_static(self, name: str) -> None:
        target = (HERE / "static" / posixpath.basename(name)).resolve()
        try:
            data = target.read_bytes()
        except OSError:
            return self._fail(500, "static file missing", str(target))
        self.send_response(200)
        self.send_header("Content-Type", _TYPES.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # -- proxy -----------------------------------------------------------

    def _upstream_headers(self, *, stream: bool) -> dict:
        headers = {
            "Authorization": "Bearer " + API_KEY,
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }
        sid = self.headers.get("X-Hermes-Session-Id")
        if sid:
            headers["X-Hermes-Session-Id"] = sid
        return headers

    def _upstream_error(self, exc: Exception) -> None:
        if isinstance(exc, urllib.error.HTTPError):
            try:
                detail = exc.read().decode("utf-8", "replace")[:2000]
            except Exception:
                detail = ""
            return self._fail(exc.code, "Hermes API server menolak request", detail)
        self._fail(
            502,
            "Hermes API server tidak bisa dihubungi di %s" % UPSTREAM,
            "Jalankan `hermes gateway restart` supaya adapter api_server hidup "
            "(port 8642). Asli: %s" % exc,
        )

    def _proxy_models(self) -> None:
        req = urllib.request.Request(UPSTREAM + "/v1/models", headers=self._upstream_headers(stream=False))
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - dilaporkan ke UI
            return self._upstream_error(exc)
        models = [m.get("id") for m in (payload.get("data") or []) if m.get("id")]
        self._json(200, {"models": models})

    def _proxy_get(self) -> None:
        """GET passthrough (path + query) for the whitelisted Hermes read-only routes."""
        target = UPSTREAM + self.path
        req = urllib.request.Request(target, headers=self._upstream_headers(stream=False))
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            return self._upstream_error(exc)
        except Exception as exc:  # noqa: BLE001 - dilaporkan ke UI
            return self._upstream_error(exc)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _proxy_chat(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._fail(400, "bad Content-Length")
        if length <= 0 or length > 16 * 1024 * 1024:
            return self._fail(400, "bad request body size")
        body = self.rfile.read(length)

        req = urllib.request.Request(
            UPSTREAM + "/v1/chat/completions", data=body, method="POST",
            headers=self._upstream_headers(stream=True),
        )
        try:
            upstream = urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - ingin pesan jelas di UI
            return self._upstream_error(exc)

        self._relay_stream(upstream, "text/event-stream")

    def _relay_stream(self, upstream, default_type: str) -> None:
        """Alirkan respons upstream apa adanya memakai chunked encoding.

        Dipakai oleh chat completions DAN /v1/runs/{id}/events. read1() penting:
        read(n) menunggu n byte penuh, jadi frame SSE tertahan sampai buffer besar
        terkumpul — persis yang membuat streaming terasa "sekali jadi".
        """
        self.send_response(getattr(upstream, "status", 200))
        self.send_header("Content-Type", upstream.headers.get("Content-Type", default_type))
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        reader = getattr(upstream, "read1", None) or upstream.read
        try:
            while True:
                chunk = reader(8192)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # user menekan Stop / menutup tab
        finally:
            upstream.close()

    def _proxy_stream_get(self) -> None:
        """GET passthrough yang mengalir (untuk stream event SSE)."""
        req = urllib.request.Request(UPSTREAM + self.path, headers=self._upstream_headers(stream=True))
        try:
            upstream = urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - ingin pesan jelas di UI
            return self._upstream_error(exc)
        self._relay_stream(upstream, "text/event-stream")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


_USAGE = """\
Pakai:
  python3 server.py                 jalankan server webchat (dipakai hermes-webchat.service)
  python3 server.py --set-password  ganti username/password secara interaktif
      opsional: --user NAMA   --random (buat password acak lalu cetak)
  python3 server.py --show-auth     tampilkan path auth.json + status kredensial
  python3 server.py --logout-all    ganti signing secret => semua sesi login hangus
"""


def cli(argv: list) -> int:
    if "--help" in argv or "-h" in argv:
        print(_USAGE)
        return 0

    if "--show-auth" in argv:
        data = load_auth()
        print("auth file :", AUTH_FILE, "(ada)" if data else "(BELUM ADA)")
        if data:
            print("username  :", data.get("username"))
            print("iterations:", data.get("iterations"))
            print("dibuat    :", data.get("created"))
        return 0

    if "--logout-all" in argv:
        data = load_auth()
        if not data:
            print("auth.json belum ada — tidak ada yang dihapus.", file=sys.stderr)
            return 1
        data["secret"] = secrets.token_hex(32)
        AUTH_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.chmod(AUTH_FILE, 0o600)
        with _auth_lock:
            _auth_cache["mtime"] = None
            _auth_cache["data"] = None
        print("Signing secret diganti — semua sesi/cookie lama tidak berlaku lagi.")
        return 0

    if "--set-password" in argv or "--passwd" in argv:
        existing = load_auth() or {}
        username = existing.get("username") or "admin"
        if "--user" in argv:
            try:
                username = argv[argv.index("--user") + 1]
            except IndexError:
                print("--user butuh nilai", file=sys.stderr)
                return 2
        if "--random" in argv or "--generate" in argv:
            password = secrets.token_urlsafe(12)
            write_auth(username, password)
            print("auth file:", AUTH_FILE)
            print("username :", username)
            print("password :", password)
            return 0
        if not sys.stdin.isatty():
            print("Mode interaktif butuh TTY. Pakai --random untuk password acak.", file=sys.stderr)
            return 2
        first = getpass.getpass("Password baru untuk %s: " % username)
        if len(first) < 8:
            print("Password minimal 8 karakter.", file=sys.stderr)
            return 2
        if first != getpass.getpass("Ulangi password: "):
            print("Tidak sama — dibatalkan.", file=sys.stderr)
            return 2
        write_auth(username, first)
        print("Password untuk %r diperbarui di %s" % (username, AUTH_FILE))
        return 0

    print(_USAGE, file=sys.stderr)
    return 2


def main() -> int:
    argv = sys.argv[1:]
    if argv:
        return cli(argv)
    if not API_KEY:
        sys.stderr.write(
            "[webchat] API_SERVER_KEY tidak ditemukan di %s — proxy akan selalu 401.\n"
            % (HERMES_HOME / ".env")
        )
    _server_start_banner()
    httpd = Server((LISTEN_HOST, LISTEN_PORT), Handler)
    sys.stderr.write("[webchat] http://%s:%d  ->  %s  (key %s)\n" % (
        LISTEN_HOST, LISTEN_PORT, UPSTREAM, "ok" if API_KEY else "MISSING"))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:  # noqa: BLE001 - biar systemd journal dapat traceback utuh
        traceback.print_exc()
        raise SystemExit(1)
