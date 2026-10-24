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
import mimetypes
import os
import posixpath
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
import zlib
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

# Upload gambar webchat — disimpan di cache lokal, bukan di-proxy ke gateway.
# Root lama dipertahankan supaya transkrip lama (`/api/image?path=`) tetap tampil.
WEBCHAT_IMAGES_ROOT = Path("/root/.hermes/cache/webchat-images")
HERMES_IMAGES_ROOT = Path("/root/.hermes/cache/images")
_IMAGE_UPLOAD_MAX = 12 * 1024 * 1024
_IMAGE_UPLOAD_SLACK = 64 * 1024
_IMAGE_RETENTION_DAYS = 14

# ---------------------------------------------------------------------------
# Lampiran (gambar + dokumen) — satu jalur unggah
#
# API server Hermes hanya menerima part teks dan image_url; "uploaded files and
# document inputs are not supported on this endpoint". Jadi file disimpan di
# sini, teksnya diekstrak (docx/xlsx/pptx/pdf/teks), lalu UI yang menyusun
# prompt teks. Gambar divalidasi dari magic bytes lalu path-nya diberikan ke
# agent supaya bisa dibaca lewat tool vision_analyze.
#
# Semua lampiran (gambar maupun dokumen) memakai satu folder `uploads/` dan satu
# endpoint POST /api/upload. `/api/image?path=` lama tetap hidup dan ikut
# menyajikan gambar dari folder ini (lihat _image_path_allowed).
# ---------------------------------------------------------------------------

UPLOAD_DIR = Path(os.environ.get("WEBCHAT_UPLOAD_DIR") or (HERE / "uploads")).resolve()
MAX_UPLOAD_MB = int(os.environ.get("WEBCHAT_MAX_UPLOAD_MB", "25"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_TEXT_CHARS = int(os.environ.get("WEBCHAT_MAX_TEXT_CHARS", "60000"))
MAX_PREVIEW_BYTES = 2 * 1024 * 1024

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
TEXT_EXT = {".txt", ".md", ".csv", ".json", ".log"}
OFFICE_EXT = {".docx", ".xlsx", ".pptx", ".pdf"}
ALLOWED_EXT = IMAGE_EXT | TEXT_EXT | OFFICE_EXT

_EXT_MIME = {
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(raw: str) -> str:
    """Nama file aman: basename saja, tanpa path, tanpa karakter aneh.

    `../../etc/passwd` -> `passwd`; `..\\..\\x.docx` -> `x.docx`.
    """
    name = (raw or "").replace("\x00", "").replace("\\", "/")
    name = name.split("/")[-1].strip()
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = name.replace(" ", "_")
    name = _UNSAFE_NAME.sub("_", name)
    stem, ext = os.path.splitext(name)
    ext = ext.lower()
    stem = stem.strip("._-") or "file"
    stem = re.sub(r"_{2,}", "_", stem)[:80]
    if ext and not re.fullmatch(r"\.[a-z0-9]{1,8}", ext):
        ext = ""
    return stem + ext


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# -- docx --------------------------------------------------------------------

def _docx_para_text(el) -> str:
    out = []
    for node in el.iter():
        tag = _local(node.tag)
        if tag == "t":
            if node.text:
                out.append(node.text)
        elif tag == "tab":
            out.append("\t")
        elif tag in ("br", "cr"):
            out.append("\n")
    return "".join(out)


def _docx_lines(root) -> list:
    """Paragraf jadi baris; tabel jadi baris tab-separated, sesuai urutan dokumen."""
    body = next((c for c in root if _local(c.tag) == "body"), root)
    lines: list = []

    def walk(el) -> None:
        for child in el:
            tag = _local(child.tag)
            if tag == "p":
                lines.append(_docx_para_text(child))
            elif tag == "tbl":
                for tr in child:
                    if _local(tr.tag) != "tr":
                        continue
                    cells = []
                    for tc in tr:
                        if _local(tc.tag) != "tc":
                            continue
                        parts = [_docx_para_text(p) for p in tc if _local(p.tag) == "p"]
                        cells.append(" ".join(x.strip() for x in parts if x.strip()))
                    if any(cells):
                        lines.append("\t".join(cells))
            elif tag in ("sdt", "sdtContent", "txbxContent"):
                walk(child)

    walk(body)
    return lines


def _extract_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    return "\n".join(_docx_lines(root))


# -- pptx --------------------------------------------------------------------

def _extract_pptx(path: Path) -> str:
    def num(name: str) -> int:
        m = re.search(r"(\d+)\.xml$", name)
        return int(m.group(1)) if m else 0

    with zipfile.ZipFile(path) as zf:
        slides = sorted((n for n in zf.namelist()
                         if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=num)
        out = []
        for i, name in enumerate(slides, 1):
            root = ET.fromstring(zf.read(name))
            out.append("## Slide %d" % i)
            for para in root.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}p"):
                text = "".join(t.text or "" for t in para.iter(
                    "{http://schemas.openxmlformats.org/drawingml/2006/main}t"))
                if text.strip():
                    out.append(text)
    return "\n".join(out)


# -- xlsx --------------------------------------------------------------------

_SML = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _extract_xlsx(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        shared: list = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in si.iter(_SML + "t"))
                      for si in root.iter(_SML + "si")]

        titles: dict = {}
        try:
            wb = ET.fromstring(zf.read("xl/workbook.xml"))
            rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            targets = {r.get("Id"): (r.get("Target") or "") for r in rels}
            for sheet in wb.iter(_SML + "sheet"):
                rid = sheet.get("{http://schemas.openxmlformats.org/officeDocument/"
                                "2006/relationships}id")
                target = targets.get(rid, "").lstrip("/")
                if target and not target.startswith("xl/"):
                    target = "xl/" + target
                titles[target] = sheet.get("name") or ""
        except (KeyError, ET.ParseError):
            pass

        def num(name: str) -> int:
            m = re.search(r"(\d+)\.xml$", name)
            return int(m.group(1)) if m else 0

        sheets = sorted((n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
                        key=num)
        out = []
        for name in sheets:
            out.append("## Sheet: " + (titles.get(name) or Path(name).stem))
            root = ET.fromstring(zf.read(name))
            for row in root.iter(_SML + "row"):
                vals = []
                for cell in row.iter(_SML + "c"):
                    kind = cell.get("t")
                    if kind == "inlineStr":
                        val = "".join(t.text or "" for t in cell.iter(_SML + "t"))
                    else:
                        node = cell.find(_SML + "v")
                        val = node.text if node is not None and node.text else ""
                        if kind == "s":
                            try:
                                val = shared[int(val)]
                            except (ValueError, IndexError):
                                pass
                    vals.append(val)
                out.append("\t".join(vals))
    return "\n".join(out)


# -- pdf ---------------------------------------------------------------------

def _extract_pdf(path: Path) -> str:
    """pdftotext kalau tersedia; kalau tidak, coba stream FlateDecode + regex Tj."""
    exe = shutil.which("pdftotext")
    if exe:
        try:
            proc = subprocess.run([exe, "-layout", "-enc", "UTF-8", str(path), "-"],
                                  capture_output=True, timeout=60)
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            pass

    try:
        data = path.read_bytes()
    except OSError:
        return ""
    pieces = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
        raw = match.group(1)
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            continue
        for token in re.finditer(rb"\((?:\\.|[^\\()])*\)", raw):
            text = token.group(0)[1:-1]
            text = text.replace(rb"\(", b"(").replace(rb"\)", b")").replace(rb"\\", b"\\")
            pieces.append(text.decode("utf-8", "replace"))
    return " ".join(p for p in pieces if p.strip())


def extract_text(path: Path, ext: str) -> tuple:
    """(teks, catatan). Tidak pernah melempar — kegagalan jadi catatan."""
    try:
        if ext in TEXT_EXT:
            return path.read_bytes().decode("utf-8", "replace").lstrip("\ufeff"), ""
        if ext == ".docx":
            return _extract_docx(path), ""
        if ext == ".xlsx":
            return _extract_xlsx(path), ""
        if ext == ".pptx":
            return _extract_pptx(path), ""
        if ext == ".pdf":
            return _extract_pdf(path), ""
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        return "", "gagal mengekstrak teks (%s: %s)" % (type(exc).__name__, exc)
    except Exception as exc:  # noqa: BLE001 - best effort, jangan sampai 500
        return "", "gagal mengekstrak teks (%s: %s)" % (type(exc).__name__, exc)
    return "", "tipe file tidak punya ekstraktor"


def build_upload_record(ident: str, name: str, target: Path, size: int) -> dict:
    ext = target.suffix.lower()
    kind = "image" if ext in IMAGE_EXT else "text"
    mime = _EXT_MIME.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"
    record = {
        "id": ident,
        "name": name,
        "mime": mime,
        "size": size,
        "bytes": size,
        "path": str(target),
        "url": "/api/uploads/" + ident,
        "kind": kind,
        "preview": "",
        "text": "",
        "truncated": False,
        "chars": 0,
        "note": "",
    }
    if kind == "image":
        if size <= MAX_PREVIEW_BYTES:
            try:
                record["preview"] = "data:%s;base64,%s" % (
                    mime, base64.b64encode(target.read_bytes()).decode("ascii"))
            except OSError:
                record["preview"] = ""
        return record

    text, note = extract_text(target, ext)
    text = text.replace("\x00", "")
    record["note"] = note
    record["chars"] = len(text)
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
        record["truncated"] = True
    record["text"] = text
    return record


def parse_multipart(body: bytes, content_type: str):
    """(fields, files) dari body multipart/form-data — parser byte-level kecil.

    Tidak memakai modul `cgi` (dihapus di Python 3.13) dan tidak memakai
    `email` (bisa mengubah byte biner); file dikembalikan apa adanya.
    """
    match = re.search(r'boundary="?([^";]+)"?', content_type or "", re.I)
    if not match:
        return None
    boundary = b"--" + match.group(1).strip().encode("latin-1", "replace")
    fields, files = {}, []
    for part in body.split(boundary)[1:]:
        if part[:2] == b"--":
            break
        if part.startswith(b"\r\n"):
            part = part[2:]
        elif part.startswith(b"\n"):
            part = part[1:]
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            head, sep, data = part.partition(b"\n\n")
        if not sep:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        elif data.endswith(b"\n"):
            data = data[:-1]
        headers = {}
        for line in head.decode("utf-8", "replace").splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        disposition = headers.get("content-disposition", "")
        field = re.search(r'name="?([^";]+)"?', disposition)
        field = field.group(1) if field else ""
        filename = re.search(r"filename\*=([^;]+)", disposition)
        if filename:
            raw = filename.group(1)
            if raw.lower().startswith("utf-8''"):
                raw = raw[7:]
            try:
                filename = urllib.parse.unquote(raw)
            except Exception:  # noqa: BLE001
                filename = raw
        else:
            filename = re.search(r'filename="?([^";]+)"?', disposition)
            filename = filename.group(1) if filename else ""
        if filename:
            files.append({"field": field, "filename": filename, "data": data,
                          "type": headers.get("content-type", "")})
        elif field:
            fields[field] = data.decode("utf-8", "replace")
    return fields, files


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


# ---------------------------------------------------------------------------
# Pemantau Agent — baca papan kanban Hermes (READ-ONLY)
#
# Halaman /agents menampilkan task kanban: status, agent yang mengerjakan, dan
# aktivitas terakhir. Sumbernya file SQLite kanban milik Hermes, bukan gateway —
# jadi tidak ada rute baru yang di-proxy dan tidak ada API key yang ikut ke
# browser. Semua koneksi dibuka `mode=ro` (URI sqlite), sehingga halaman ini
# tidak mungkin menulis ke papan.
#
# Lokasi (meniru hermes_cli.kanban_db): board `default` = <root>/kanban.db,
# board lain = <root>/kanban/boards/<slug>/kanban.db, board aktif dicatat di
# <root>/kanban/current. Pin `HERMES_KANBAN_DB` / `HERMES_KANBAN_BOARD` sengaja
# tidak dipakai: itu penanda worker (worker hanya boleh melihat satu papan),
# sementara halaman ini memang ingin melihat semua papan.
# ---------------------------------------------------------------------------

BOARD_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
AGENT_STATUS_KEYS = ("running", "ready", "todo", "blocked", "review", "done", "archived")
AGENT_DEFAULT_LIMIT = 150
AGENT_MAX_LIMIT = 200
AGENT_PREVIEW_CHARS = 400
AGENT_COMMENT_PREVIEW = 400
AGENT_EVENT_PREVIEW = 240
AGENT_BODY_MAX = 20000
AGENT_DETAIL_COMMENTS = 10
AGENT_DETAIL_EVENTS = 20
AGENT_DETAIL_RUNS = 5

# --- Workspace (kantor virtual) --------------------------------------------
# Satu meja per profil; state diturunkan dari kanban.db + /proc, bukan dari
# laporan agen itu sendiri. Aturannya sengaja kaku supaya bisa diuji:
#   working — ada task `running` untuk profil itu DAN `worker_pid`-nya hidup.
#   blocked — ada task `blocked` untuk profil itu (menang atas idle).
#   done    — task terakhir selesai < AGENT_LIVE_DONE_WINDOW detik lalu (bubble
#             "selesai" hanya muncul sebentar, setelah itu kembali idle).
#   idle    — profil terdaftar, tapi tidak memenuhi satu pun di atas.
#   offline — assignee muncul di papan tapi tidak punya direktori profil.
AGENT_LIVE_STATES = ("working", "blocked", "done", "idle", "offline")
AGENT_LIVE_RECENT = 3
AGENT_LIVE_RECENT_CHARS = 80
AGENT_LIVE_TITLE_CHARS = 140
AGENT_LIVE_ROLE_CHARS = 80
AGENT_LIVE_DONE_WINDOW = 120
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _hermes_root() -> Path:
    """Root instalasi Hermes (tempat `kanban.db` board default).

    Meniru `hermes_constants.get_default_hermes_root()`: HERMES_HOME yang menunjuk
    `<root>/profiles/<nama>` dipetakan balik ke `<root>`, sedangkan HERMES_HOME
    biasa di dalam `~/.hermes` tetap memakai `~/.hermes`.
    """
    native = Path(os.path.expanduser("~")) / ".hermes"
    env_home = (os.environ.get("HERMES_HOME") or "").strip()
    if not env_home:
        return native
    candidate = Path(os.path.expanduser(env_home))
    try:
        candidate.resolve().relative_to(native.resolve())
        return native
    except (ValueError, OSError):
        if candidate.parent.name == "profiles":
            return candidate.parent.parent
        return candidate


def _kanban_home() -> Path:
    override = (os.environ.get("HERMES_KANBAN_HOME") or "").strip()
    return Path(os.path.expanduser(override)) if override else _hermes_root()


def _boards_root() -> Path:
    return _kanban_home() / "kanban" / "boards"


def _board_dir(slug: str) -> Path:
    return _boards_root() / slug


def _board_db_path(slug: str) -> Path:
    """`kanban.db` satu board. Board `default` tetap di `<root>/kanban.db`."""
    if slug == "default":
        return _kanban_home() / "kanban.db"
    root = _boards_root().resolve()
    target = (_boards_root() / slug / "kanban.db").resolve()
    try:
        # Sabuk pengaman kedua: slug sudah divalidasi regex, tapi seandainya ada
        # jalur yang lolos, file yang dibaca tidak boleh keluar dari boards/.
        target.relative_to(root)
    except ValueError:
        raise ValueError("slug board keluar dari direktori boards") from None
    return target


def _display_name(slug: str) -> str:
    """`atm10-server` -> `Atm10 Server` (sama seperti CLI)."""
    return " ".join(w.capitalize() for w in slug.replace("_", "-").split("-") if w) or slug


def _board_meta(slug: str) -> dict:
    """Metadata board dari `board.json`; tidak pernah melempar error."""
    meta = {"slug": slug, "name": _display_name(slug), "description": "", "archived": False}
    try:
        raw = json.loads((_board_dir(slug) / "board.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return meta
    if isinstance(raw, dict):
        if raw.get("name"):
            meta["name"] = str(raw["name"])
        if raw.get("description"):
            meta["description"] = str(raw["description"])
        meta["archived"] = bool(raw.get("archived"))
    return meta


def _board_exists(slug: str) -> bool:
    """`default` selalu ada; board lain wajib punya `board.json` (penanda identitas)."""
    if slug == "default":
        return True
    return (_board_dir(slug) / "board.json").is_file()


def _current_board() -> str:
    """Slug board aktif dari `<root>/kanban/current`; kosong/basi -> `default`."""
    try:
        raw = (_kanban_home() / "kanban" / "current").read_text(encoding="utf-8-sig").strip().lower()
    except OSError:
        return "default"
    if BOARD_SLUG_RE.match(raw) and _board_exists(raw) and not _board_meta(raw).get("archived"):
        return raw
    return "default"


def list_board_meta() -> list:
    """`default` lebih dulu, lalu `boards/<slug>/` yang punya `board.json`, terurut."""
    metas = [_board_meta("default")]
    root = _boards_root()
    if not root.is_dir():
        return metas
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name == "default" or not BOARD_SLUG_RE.match(child.name):
            continue
        if not (child / "board.json").is_file():
            continue                      # direktori sisa (bukan board)
        meta = _board_meta(child.name)
        if meta.get("archived"):
            continue                      # tombstone hasil `boards rm`
        metas.append(meta)
    return metas


def _open_board_db(db: Path):
    """Koneksi sqlite3 read-only. `mode=ro` = jaminan tidak ada tulisan."""
    conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=1.5)
    conn.row_factory = sqlite3.Row
    return conn


def _board_query(db: Path, work):
    """Jalankan `work(conn)`, ulangi sekali kalau DB sedang terkunci (WAL)."""
    for attempt in (1, 2):
        conn = None
        try:
            conn = _open_board_db(db)
            return work(conn)
        except sqlite3.OperationalError:
            if attempt == 1:
                time.sleep(0.15)
                continue
            raise
        except sqlite3.Error as exc:
            raise RuntimeError("kanban.db tidak bisa dibaca: %s" % exc) from exc
        finally:
            if conn is not None:
                conn.close()
    raise RuntimeError("kanban.db tidak bisa dibaca")


def _placeholder(values) -> str:
    return "(" + ",".join("?" for _ in values) + ")"


def _status_counts(slug: str) -> dict:
    """`{status: jumlah}`; `{}` kalau DB-nya belum ada (board kosong)."""
    db = _board_db_path(slug)
    if not db.is_file():
        return {}

    def work(conn):
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
        return {str(r["status"]): int(r["n"]) for r in rows}

    try:
        return _board_query(db, work)
    except (sqlite3.Error, RuntimeError, ValueError):
        return {}


def _agent_limit(query: dict) -> int:
    raw = (query.get("limit") or [""])[0].strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return AGENT_DEFAULT_LIMIT
    return max(1, min(AGENT_MAX_LIMIT, value))


# ---------------------------------------------------------------------------
# Papan kanban: jalur TULIS lewat CLI `hermes kanban`
#
# Kenapa subprocess, bukan INSERT/UPDATE ke kanban.db: transisi status bukan
# sekadar kolom. `complete` menutup run worker, `promote` menaikkan task ke
# ready, `block` mencatat block_kind + event, dan dispatcher gateway memakai
# invariant yang sama. Menulis DB sendiri berarti melewati claim/lock, event,
# dan hook lifecycle — board akan tampak berubah tapi worker tidak pernah
# dijadwalkan. Jadi CLI adalah satu-satunya jalur mutasi.
#
# Keamanan:
#   * argv selalu LIST (tanpa shell=True) → tidak ada string yang dirakit jadi
#     perintah; nilai dari klien hanya masuk sebagai satu argumen.
#   * flag TIDAK pernah berasal dari klien; klien hanya memilih aksi dari
#     whitelist, server yang memetakan ke flag tetap.
#   * env child disusun eksplisit (allowlist) → tidak ada API key/sesi gateway
#     yang ikut ke proses anak.
#   * batas waktu subprocess + stderr dipotong & di-scrub sebelum dikirim balik.
# ---------------------------------------------------------------------------

KANBAN_BIN = (os.environ.get("WEBCHAT_KANBAN_BIN")
              or "/root/.hermes/hermes-agent/.hermes/bin/hermes")
KANBAN_TIMEOUT = float(os.environ.get("WEBCHAT_KANBAN_TIMEOUT", "60"))
AGENT_TITLE_MAX = 200
AGENT_COMMENT_MAX = 8000
AGENT_REASON_MAX = 500
AGENT_RESULT_MAX = 4000
AGENT_SUMMARY_MAX = 4000
AGENT_JSON_MAX = 96 * 1024
AGENT_PRIORITY_MIN, AGENT_PRIORITY_MAX = 0, 5
AGENT_MUTATE_LIMIT = int(os.environ.get("WEBCHAT_MUTATE_LIMIT", "40"))
AGENT_MUTATE_WINDOW = 60
AGENT_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,120}$")
AGENT_PROVIDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
AGENT_ASSIGNEE_TTL = 30.0
KANBAN_ACTIONS = ("assign", "promote", "block", "unblock", "complete", "archive",
                  "review", "reopen", "comment", "edit", "set-model")
KANBAN_BLOCK_KINDS = ("capability", "dependency", "needs_input", "transient")
AGENT_WORKSPACE_KINDS = ("scratch", "worktree")

# --- Otomasi (cron/skills/toolsets/status/artifact) -------------------------
# Semua nilai dari klien divalidasi bentuknya SEBELUM ditempel ke path upstream:
# id job cron selalu 12 hex, id sesi & artifact dibatasi karakter aman, jadi
# tidak ada kesempatan menyelipkan "../" atau query tambahan ke URL gateway.
JOB_ID_RE = re.compile(r"^[a-f0-9]{12}$")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,127}$")
ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
JOB_MUTATION_RE = re.compile(r"^/api/jobs/([a-f0-9]{12})/(pause|resume|run)$")
JOB_ITEM_RE = re.compile(r"^/api/jobs/([a-f0-9]{12})$")
SESSION_ITEM_RE = re.compile(r"^/api/sessions/([^/]+)$")
SESSION_ACTION_RE = re.compile(r"^/api/sessions/([^/]+)/(fork|model)$")
ARTIFACT_DL_RE = re.compile(r"^/v1/artifacts/download/([A-Za-z0-9][A-Za-z0-9_.-]{0,127})$")
ARTIFACT_MAX_BYTES = 16 * 1024 * 1024
OTOMASI_JSON_MAX = 64 * 1024
STARTED_AT = time.time()

_assignee_lock = threading.Lock()
_assignee_cache: dict = {"at": 0.0, "items": []}
_mutate_lock = threading.Lock()
_mutate_hits: dict = {}


class KanbanError(Exception):
    """Kesalahan yang sudah dipetakan ke kode HTTP + pesan untuk klien."""

    def __init__(self, status: int, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.status = int(status)
        self.message = message
        self.detail = detail


def board_slug_problem(slug: str) -> str:
    """Pesan kesalahan slug, atau string kosong kalau sah."""
    if not slug:
        return "Parameter slug wajib diisi."
    if not BOARD_SLUG_RE.match(slug):
        return ("Slug hanya boleh huruf kecil, angka, tanda hubung, dan garis bawah "
                "(maksimal 64 karakter, tidak diawali tanda hubung).")
    return ""


def _kanban_env() -> dict:
    """Env minimal untuk CLI: HERMES_HOME eksplisit, tanpa kredensial gateway."""
    return {
        "HERMES_HOME": str(HERMES_HOME),
        "HOME": os.environ.get("HOME") or str(Path.home()),
        "PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin",
        "LANG": os.environ.get("LANG") or "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "TERM": "dumb",
    }


def _kanban_scrub(text: str, limit: int = 600) -> str:
    """Potong + samarkan kemungkinan rahasia sebelum teks CLI dikirim ke klien."""
    value = (text or "").strip()
    value = re.sub(r"(?i)\b(api[_-]?key|token|secret|password|bearer)\b\s*[=:]?\s*\S+",
                   r"\1=***", value)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "sk-***", value)
    return value[:limit]


def _kanban_line(text: str) -> str:
    """Baris pertama yang tidak kosong — dipakai sebagai pesan error singkat."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return _kanban_scrub(line, 300)
    return ""


def _kanban_launcher_python(binary: Path) -> Path | None:
    """Interpreter yang dieksekusi launcher shell `hermes` (baris `exec …`), kalau ada."""
    try:
        lines = binary.read_text(encoding="utf-8", errors="replace").splitlines()[:4]
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line.startswith("exec "):
            continue
        token = line[5:].split()[0].strip("'\"")
        if token.startswith("/"):
            return Path(token)
    return None


def _kanban_store_python() -> Path | None:
    """Python store milik home ini (`<root>/tools/python-*/bin/python3`)."""
    try:
        for path in sorted((_hermes_root() / "tools").glob("python-*/bin/python3")):
            if path.is_file():
                return path
    except OSError:
        return None
    return None


def _kanban_bootstrap(root: Path) -> str:
    """Kode `-c` yang setara dengan ekor launcher: jalankan `hermes_cli.main`."""
    return ("import sys; sys.path.insert(0, %r); from hermes_cli.main import main; "
            "sys.argv[0] = 'hermes'; raise SystemExit(main())" % str(root))


def kanban_run(args: list, *, timeout: float = KANBAN_TIMEOUT,
               stdin_text: str | None = None) -> subprocess.CompletedProcess:
    """Jalankan `hermes kanban <args>` dengan argv list. Tidak pernah shell=True."""
    binary = Path(KANBAN_BIN)
    if not binary.is_file():
        raise KanbanError(503, "CLI kanban tidak ditemukan",
                          "Biner tidak ada di %s. Set WEBCHAT_KANBAN_BIN." % KANBAN_BIN)
    argv = [str(binary), "kanban"] + [str(a) for a in args]
    interpreter = _kanban_launcher_python(binary)
    if interpreter is not None and not interpreter.is_file():
        # Launcher shell menunjuk interpreter yang sudah tidak ada (pernah ditulis
        # ulang oleh proses lain yang memakai HERMES_HOME sementara). Jangan
        # menyentuh file launcher bersama itu: jalankan modul yang sama lewat
        # python store milik home ini.
        store = _kanban_store_python()
        if store is None:
            raise KanbanError(
                503, "launcher `hermes` menunjuk interpreter yang hilang",
                "%s tidak ada. Jalankan `hermes` sekali dari shell untuk memperbaiki launcher."
                % interpreter)
        argv = [str(store), "-c", _kanban_bootstrap(_hermes_root() / "hermes-agent"),
                "kanban"] + [str(a) for a in args]
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_kanban_env(),
            cwd=str(HERMES_HOME),
            input="" if stdin_text is None else stdin_text,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise KanbanError(
            504, "perintah kanban melebihi %.0f detik" % timeout,
            "Task mungkin masih diproses. Muat ulang papan, lalu cek "
            "`hermes kanban diagnostics` kalau berulang.",
        ) from exc
    except OSError as exc:
        raise KanbanError(502, "CLI kanban gagal dijalankan",
                          _kanban_scrub("%s: %s" % (type(exc).__name__, exc))) from exc


def kanban_json(args: list, *, timeout: float = KANBAN_TIMEOUT,
                stdin_text: str | None = None):
    """Jalankan perintah kanban yang mendukung `--json` dan kembalikan hasil parse."""
    proc = kanban_run(args, timeout=timeout, stdin_text=stdin_text)
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        raise KanbanError(409, _kanban_line(err or out) or "kanban menolak perintah",
                          _kanban_scrub((err + "\n" + out).strip()))
    try:
        return json.loads(out)
    except ValueError:
        raise KanbanError(502, "keluaran CLI kanban bukan JSON",
                          _kanban_scrub(out or (proc.stderr or ""))) from None


def kanban_assignee_items(*, force: bool = False) -> list:
    """Daftar profil assignee dari CLI, di-cache sebentar (tiap panggilan ~4 dtk)."""
    now = time.time()
    with _assignee_lock:
        cached = list(_assignee_cache["items"])
        fresh = (now - float(_assignee_cache["at"] or 0)) < AGENT_ASSIGNEE_TTL
    if cached and fresh and not force:
        return cached
    data = kanban_json(["assignees", "--json"])
    if not isinstance(data, list):
        raise KanbanError(502, "keluaran `kanban assignees` tidak dikenali",
                          _kanban_scrub(str(data)))
    items = [item for item in data if isinstance(item, dict) and item.get("name")]
    with _assignee_lock:
        _assignee_cache["at"] = time.time()
        _assignee_cache["items"] = items
    return list(items)


def _kanban_assignee(value, *, required: bool = False) -> str:
    """Validasi nama profil terhadap daftar assignee CLI. 'none' = lepas assignee."""
    text = str(value or "").strip()
    if not text:
        if required:
            raise KanbanError(400, "assignee wajib diisi", "Pilih profil dari daftar.")
        return ""
    if text.lower() == "none":
        return "none"
    names = [str(i.get("name")) for i in kanban_assignee_items()]
    if text not in names:
        raise KanbanError(400, "profil '%s' tidak dikenal" % text,
                          "Profil tersedia: " + (", ".join(names) or "(belum ada profil)"))
    return text


def _kanban_text(value, field: str, *, max_len: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise KanbanError(400, "%s harus berupa teks" % field)
    if "\x00" in value:
        raise KanbanError(400, "%s mengandung karakter terlarang" % field)
    text = value.strip()
    if len(text) > max_len:
        raise KanbanError(400, "%s maksimal %d karakter (sekarang %d)"
                          % (field, max_len, len(text)))
    if required and not text:
        raise KanbanError(400, "%s wajib diisi" % field)
    return text


def _kanban_title(value) -> str:
    text = _kanban_text(value, "judul", max_len=AGENT_TITLE_MAX, required=True)
    if text.startswith("-"):
        # argv list tidak di-escape; judul berawalan '-' bisa dibaca CLI sebagai flag.
        raise KanbanError(400, "judul tidak boleh diawali tanda hubung",
                          "Awali dengan huruf atau angka.")
    return text


def _kanban_priority(value) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        raise KanbanError(400, "priority harus angka bulat 0–5")
    if isinstance(value, str) and value.strip().lstrip("+-").isdigit():
        value = int(value.strip())
    if not isinstance(value, int):
        raise KanbanError(400, "priority harus angka bulat 0–5")
    if not AGENT_PRIORITY_MIN <= value <= AGENT_PRIORITY_MAX:
        raise KanbanError(400, "priority harus di antara 0 dan 5", "Diterima: %d" % value)
    return int(value)


def _kanban_workspace(value) -> str:
    text = str(value or "scratch").strip()
    if text in AGENT_WORKSPACE_KINDS:
        return text
    for prefix in ("worktree:", "dir:"):
        if text.startswith(prefix):
            path = text[len(prefix):].strip()
            parts = path.split("/")
            if (not path.startswith("/") or "\x00" in path or ".." in parts
                    or len(text) > 400):
                raise KanbanError(400, "path workspace tidak valid",
                                  "Harus path absolut tanpa '..', maksimal 400 karakter.")
            return text
    raise KanbanError(400, "workspace tidak dikenal",
                      "Pilih scratch, worktree, worktree:<path absolut>, atau dir:<path absolut>.")


def _kanban_model(value) -> str:
    text = str(value or "").strip()
    if not text:
        raise KanbanError(400, "model wajib diisi", "Kirim 'none' untuk melepas override.")
    if text.lower() == "none":
        return "none"
    if not AGENT_MODEL_RE.match(text):
        raise KanbanError(400, "nama model tidak valid",
                          "Huruf/angka serta . _ : / @ + - (maks 121 karakter).")
    return text


def _kanban_block_kind(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text not in KANBAN_BLOCK_KINDS:
        raise KanbanError(400, "jenis block tidak dikenal",
                          "Pilih salah satu: " + ", ".join(KANBAN_BLOCK_KINDS))
    return text


def _kanban_positional(value: str) -> list:
    """Argumen posisional bebas: kalau berawalan '-', sisipkan pemisah `--`."""
    return (["--", value] if value.startswith("-") else [value])


def kanban_task_args(action: str, task_id: str, data: dict) -> list:
    """Peta aksi klien → argv tetap. Tidak ada flag yang berasal dari klien."""
    if action == "assign":
        who = _kanban_assignee(data.get("assignee"), required=True)
        return ["assign", task_id, who]
    if action == "promote":
        args = ["promote", task_id]
        reason = _kanban_text(data.get("reason"), "alasan", max_len=AGENT_REASON_MAX)
        return args + _kanban_positional(reason) if reason else args
    if action == "block":
        kind = _kanban_block_kind(data.get("kind"))
        reason = _kanban_text(data.get("reason"), "alasan", max_len=AGENT_REASON_MAX,
                              required=True)
        args = ["block"]
        if kind:
            args.append("--kind=" + kind)
        args.append(task_id)
        return args + _kanban_positional(reason)
    if action == "unblock":
        args = ["unblock", task_id]
        reason = _kanban_text(data.get("reason"), "alasan", max_len=AGENT_REASON_MAX)
        if reason:
            args = ["unblock", "--reason=" + reason, task_id]
        return args
    if action == "complete":
        result = _kanban_text(data.get("result"), "result", max_len=AGENT_RESULT_MAX)
        args = ["complete"]
        if result:
            args.append("--result=" + result)
        if bool(data.get("force")):
            args.append("--force")
        return args + [task_id]
    if action == "archive":
        return ["archive", task_id]
    if action == "review":
        args = ["request-review"]
        summary = _kanban_text(data.get("summary"), "ringkasan", max_len=AGENT_SUMMARY_MAX)
        if summary:
            args.append("--summary=" + summary)
        if data.get("reviewer"):
            args.append("--reviewer=" + _kanban_assignee(data.get("reviewer"), required=True))
        if bool(data.get("force")):
            args.append("--force")
        return args + [task_id]
    if action == "reopen":
        args = ["reopen-review"]
        reason = _kanban_text(data.get("reason"), "alasan", max_len=AGENT_REASON_MAX)
        if reason:
            args.append("--reason=" + reason)
        return args + [task_id]
    if action == "comment":
        text = _kanban_text(data.get("text"), "komentar", max_len=AGENT_COMMENT_MAX,
                            required=True)
        return ["comment", task_id] + _kanban_positional(text)
    if action == "edit":
        args = ["edit", task_id]
        touched = False
        if "title" in data and data.get("title") not in (None, ""):
            args.append("--title=" + _kanban_title(data.get("title")))
            touched = True
        if "body" in data and data.get("body") is not None:
            args.append("--body=" + _kanban_text(data.get("body"), "deskripsi",
                                                 max_len=AGENT_BODY_MAX))
            touched = True
        if "priority" in data and data.get("priority") not in (None, ""):
            args.append("--priority=%d" % _kanban_priority(data.get("priority")))
            touched = True
        if not touched:
            raise KanbanError(400, "tidak ada yang diedit",
                              "Kirim minimal satu dari title, body, atau priority.")
        return args
    if action == "set-model":
        args = ["set-model", task_id, _kanban_model(data.get("model"))]
        provider = str(data.get("provider") or "").strip()
        if provider:
            if not AGENT_PROVIDER_RE.match(provider):
                raise KanbanError(400, "nama provider tidak valid",
                                  "Huruf/angka serta . _ - (maks 64 karakter).")
            args = ["set-model", "--provider=" + provider, task_id,
                    _kanban_model(data.get("model"))]
        return args
    raise KanbanError(400, "action tidak dikenal",
                      "Pilih salah satu: " + ", ".join(KANBAN_ACTIONS))


def kanban_mutate(slug: str, action: str, task_id: str, data: dict) -> dict:
    """Jalankan satu mutasi task; kembalikan pesan sukses dari CLI apa adanya."""
    args = ["--board", slug] + kanban_task_args(action, task_id, data)
    proc = kanban_run(args)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        raise KanbanError(409, _kanban_line(err or out) or "kanban menolak aksi",
                          _kanban_scrub((err + "\n" + out).strip()))
    return {"message": _kanban_scrub(out or err, 500)}


def kanban_create(slug: str, data: dict) -> dict:
    """Buat task lewat `kanban create`; body dikirim via stdin (aman untuk '-')."""
    title = _kanban_title(data.get("title"))
    body = _kanban_text(data.get("body"), "deskripsi", max_len=AGENT_BODY_MAX)
    priority = _kanban_priority(data.get("priority"))
    workspace = _kanban_workspace(data.get("workspace"))
    args = ["--board", slug, "create", title, "--body-file=-",
            "--priority=%d" % priority, "--workspace=" + workspace, "--json"]
    assignee = _kanban_assignee(data.get("assignee"))
    if assignee and assignee != "none":
        args.append("--assignee=" + assignee)
    parent = str(data.get("parent") or "").strip()
    if parent:
        if not TASK_ID_RE.match(parent):
            raise KanbanError(400, "id task induk tidak valid", "Contoh: t_1a2b3c4d")
        if agent_task_payload(slug, parent) is None:
            raise KanbanError(404, "task induk tidak ditemukan", parent)
        # Anak dari task yang belum selesai lahir di `todo`, bukan `ready`.
        args.append("--parent=" + parent)
    if bool(data.get("triage")):
        args.append("--triage")
    payload = kanban_json(args, stdin_text=body)
    task_id = str(payload.get("id") or "") if isinstance(payload, dict) else ""
    if task_id and TASK_ID_RE.match(task_id):
        fresh = agent_task_payload(slug, task_id)
        if fresh and isinstance(fresh.get("task"), dict):
            return fresh["task"]
    if not isinstance(payload, dict):
        raise KanbanError(502, "keluaran `kanban create` tidak dikenali",
                          _kanban_scrub(str(payload)))
    return payload


def kanban_board_create(data: dict) -> dict:
    """Buat board baru (`kanban boards create`); tanpa mengubah board aktif."""
    slug = str(data.get("slug") or "").strip().lower()
    problem = board_slug_problem(slug)
    if problem:
        raise KanbanError(400, "slug board tidak valid", problem)
    name = _kanban_text(data.get("name"), "nama board", max_len=80) or _display_name(slug)
    args = ["boards", "create", slug, "--name=" + name]
    proc = kanban_run(args)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        raise KanbanError(409, _kanban_line(err or out) or "board gagal dibuat",
                          _kanban_scrub((err + "\n" + out).strip()))
    return {"board": _board_meta(slug), "message": _kanban_scrub(out, 500)}


def mutate_allowed(key: str) -> bool:
    """Rate limit sederhana per pengguna+IP: AGENT_MUTATE_LIMIT aksi / menit."""
    now = time.time()
    with _mutate_lock:
        hits = [t for t in _mutate_hits.get(key, []) if now - t < AGENT_MUTATE_WINDOW]
        if len(hits) >= AGENT_MUTATE_LIMIT:
            _mutate_hits[key] = hits
            return False
        hits.append(now)
        _mutate_hits[key] = hits
        if len(_mutate_hits) > 500:                  # buang kunci basi
            for stale in [k for k, v in _mutate_hits.items() if not v or now - v[-1] > 300]:
                _mutate_hits.pop(stale, None)
        return True


def _pid_alive(pid) -> bool:
    try:
        return bool(pid) and Path("/proc/%d" % int(pid)).is_dir()
    except (TypeError, ValueError):
        return False


# --- Workspace: daftar profil + state live ---------------------------------

def _profiles_root() -> Path:
    return _hermes_root() / "profiles"


def _profile_role(path: Path) -> str:
    """Deskripsi singkat profil dari `profile.yaml` (kunci `description:`).

    Parser mini tanpa PyYAML: ambil baris `description:` beserta baris lanjutan
    yang menjorok, lalu potong. Kalau tidak ada, role dikosongkan — UI memakai
    nama profil sebagai judul meja.
    """
    try:
        lines = (path / "profile.yaml").read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return ""
    for index, line in enumerate(lines):
        if not line.startswith("description:"):
            continue
        parts = [line.split(":", 1)[1].strip()]
        for cont in lines[index + 1:]:
            if not cont[:1].isspace():
                break                     # kunci baru
            parts.append(cont.strip())
        return " ".join(p for p in parts if p)[:AGENT_LIVE_ROLE_CHARS]
    return ""


def list_profiles() -> list:
    """Profil Hermes yang punya meja di kantor: `default` + `<root>/profiles/*`.

    Direktori non-profil (cache, backups, home, …) dilewati: penandanya harus ada
    `config.yaml` atau `profile.yaml`.
    """
    out = [{"profile": "default", "role": "", "home": True}]
    root = _profiles_root()
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or not PROFILE_NAME_RE.match(child.name):
            continue
        if not (child / "config.yaml").is_file() and not (child / "profile.yaml").is_file():
            continue
        out.append({"profile": child.name, "role": _profile_role(child), "home": True})
    return out


def _live_task_fields(row) -> dict:
    """Kolom task yang dibutuhkan kantor virtual (tanpa body — isi task tidak
    pernah dikirim ke browser)."""
    return {
        "id": str(row["id"]),
        "title": str(row["title"] or ""),
        "status": str(row["status"]),
        "worker_pid": row["worker_pid"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "created_at": row["created_at"],
        "block_kind": row["block_kind"],
        "last_heartbeat_at": row["last_heartbeat_at"],
    }


def _live_when(task: dict | None, *keys) -> int | None:
    """Timestamp pertama yang terisi dari `keys` — `None` kalau tidak ada."""
    if not task:
        return None
    for key in keys:
        value = task.get(key)
        if not value:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _live_recent(conn, ids: list) -> dict:
    """`{task_id: ["komentar/event terakhir", …]}` maksimal AGENT_LIVE_RECENT
    entri per task, tiap entri dipotong AGENT_LIVE_RECENT_CHARS karakter.

    Komentar dan event non-heartbeat didahulukan (itu isi aktivitas sebenarnya);
    `heartbeat` hanya dipakai untuk mengisi sisa slot, supaya "aktivitas terakhir"
    tidak penuh dengan denyut liveness.
    """
    out: dict = {}
    if not ids:
        return out
    where = _placeholder(ids)
    rows = conn.execute(
        "SELECT task_id, created_at, 'comment' AS kind, COALESCE(author, '') AS who, "
        "COALESCE(body, '') AS text FROM task_comments WHERE task_id IN " + where +
        " UNION ALL "
        "SELECT task_id, created_at, 'event' AS kind, '' AS who, COALESCE(kind, '') AS text "
        "FROM task_events WHERE task_id IN " + where +
        " ORDER BY created_at DESC LIMIT 400",
        list(ids) + list(ids)).fetchall()
    main: dict = {}
    beats: dict = {}
    for r in rows:
        tid = str(r["task_id"])
        text = " ".join(str(r["text"] or "").split())
        is_event = str(r["kind"]) == "event"
        if is_event:
            text = "event: " + text
            bucket = beats.setdefault(tid, [])
        else:
            if r["who"]:
                text = str(r["who"]) + ": " + text
            bucket = main.setdefault(tid, [])
        text = text[:AGENT_LIVE_RECENT_CHARS].strip()
        if text and text != "event:":
            bucket.append(text)
    for tid in set(list(main) + list(beats)):
        items = main.get(tid, [])[:AGENT_LIVE_RECENT]
        if len(items) < AGENT_LIVE_RECENT:
            items += beats.get(tid, [])[:AGENT_LIVE_RECENT - len(items)]
        out[tid] = items
    return out


def _live_last_event(conn, ids: list) -> dict:
    """Event terakhir per task (`kind` + waktu) untuk bubble/notifikasi."""
    out: dict = {}
    if not ids:
        return out
    rows = conn.execute(
        "SELECT task_id, kind, created_at FROM task_events WHERE id IN "
        "(SELECT MAX(id) FROM task_events WHERE task_id IN %s GROUP BY task_id)"
        % _placeholder(ids), list(ids))
    for r in rows:
        out[str(r["task_id"])] = {"kind": str(r["kind"] or ""), "at": r["created_at"]}
    return out


def _live_facts(conn) -> dict:
    """Satu query pass: task terbaru per (assignee, status) + hitungan `running`.

    Daftar tanpa body supaya murah dipanggil tiap 3 detik oleh /api/agents/live.
    """
    rows = conn.execute(
        "SELECT id, title, assignee, status, worker_pid, started_at, completed_at, "
        "created_at, block_kind, last_heartbeat_at FROM tasks "
        "WHERE status IN ('running', 'blocked', 'done') AND assignee IS NOT NULL "
        "AND assignee <> '' "
        "ORDER BY COALESCE(started_at, created_at) DESC, id DESC").fetchall()
    latest: dict = {}
    picked: list = []
    running_total = 0
    running_alive = 0
    running_by: dict = {}
    for row in rows:
        who = str(row["assignee"])
        status = str(row["status"])
        slot = latest.setdefault(who, {})
        if status not in slot:                # order by aktivitas: yang pertama = terbaru
            slot[status] = _live_task_fields(row)
            picked.append(str(row["id"]))
        if status == "running":
            running_total += 1
            running_by[who] = running_by.get(who, 0) + 1
            if _pid_alive(row["worker_pid"]):
                running_alive += 1
    return {"latest": latest, "picked": picked, "running_total": running_total,
            "running_alive": running_alive, "running_by": running_by}


def _live_agent(profile: dict, slot: dict, recent: dict, events: dict, now: int) -> dict:
    """Turunkan state satu agen dari fakta papan (lihat komentar AGENT_LIVE_STATES)."""
    who = profile["profile"]
    running, blocked, done = slot.get("running"), slot.get("blocked"), slot.get("done")
    pid = running["worker_pid"] if running else None
    alive = _pid_alive(pid)
    task = None
    state = "idle"
    since = None
    if running is not None and alive:
        state, task = "working", running
        since = _live_when(running, "started_at", "created_at")
    elif blocked is not None:
        state, task = "blocked", blocked
        event = events.get(blocked["id"]) or {}
        kind = str(event.get("kind") or "")
        since = int(event["at"]) if "block" in kind and event.get("at") else \
            _live_when(blocked, "started_at", "created_at")
    elif done is not None and _live_when(done, "completed_at") and \
            now - int(done["completed_at"]) <= AGENT_LIVE_DONE_WINDOW:
        state, task = "done", done
        since = _live_when(done, "completed_at")
    elif not profile.get("home", True):
        state, task = "offline", None
    else:
        # idle: "sejak" = aktivitas terakhir yang kita ketahui dari task terakhir
        # (heartbeat terakhir kalau worker-nya sudah mati, completed_at kalau selesai).
        task = running or blocked or done
        since = _live_when(task, "completed_at", "last_heartbeat_at", "started_at", "created_at")
    if task is None:
        task_pid = pid
    else:
        task_pid = task["worker_pid"] or pid
    return {
        "profile": who,
        "role": profile.get("role") or "",
        "state": state,
        "task_id": task["id"] if task else None,
        "task_title": (task["title"] if task else "")[:AGENT_LIVE_TITLE_CHARS],
        "task_status": task["status"] if task else None,
        "since": since,
        "worker_pid": task_pid,
        "worker_alive": bool(alive),
        "last_event": events.get(task["id"]) if task else None,
        "recent": recent.get(task["id"], []) if task else [],
        "block_kind": (task.get("block_kind") if task else None),
    }


def agent_live_payload(slug: str) -> dict:
    """Payload `GET /api/agents/live` — kantor virtual (state per profil).

    `counts.working` sengaja dihitung per TASK: jumlah task `running` yang
    `worker_pid`-nya benar-benar hidup (itulah yang bisa dibandingkan langsung
    dengan `hermes kanban --board <slug> list`). `counts.working_agents` =
    jumlah meja yang berstatus bekerja — beda hanya kalau satu profil memegang
    dua task `running` sekaligus.
    """
    info = _board_meta(slug)
    info.pop("archived", None)
    info["current"] = bool(slug == _current_board())
    now = int(time.time())
    profiles = list_profiles()
    known = {p["profile"] for p in profiles}
    facts = {"latest": {}, "picked": [], "running_total": 0, "running_alive": 0, "running_by": {}}
    recent: dict = {}
    events: dict = {}

    db = _board_db_path(slug)
    has_db = db.is_file()
    if has_db:
        facts = _board_query(db, _live_facts)
        picked = facts["picked"]
        if picked:
            def activity(conn):
                return _live_recent(conn, picked), _live_last_event(conn, picked)
            recent, events = _board_query(db, activity)

    # Assignee yang muncul di papan tapi tidak punya direktori profil → kursi
    # kosong ("offline"), bukan disembunyikan.
    for who in sorted(facts["latest"]):
        if who not in known:
            profiles.append({"profile": who, "role": "", "home": False})

    agents = []
    counts = {key: 0 for key in AGENT_LIVE_STATES}
    for profile in sorted(profiles, key=lambda p: p["profile"].lower()):
        agent = _live_agent(profile, facts["latest"].get(profile["profile"], {}),
                            recent, events, now)
        agent["tasks_running"] = int(facts["running_by"].get(profile["profile"], 0))
        counts[agent["state"]] = counts.get(agent["state"], 0) + 1
        agents.append(agent)
    counts["working_agents"] = counts.get("working", 0)
    counts["working"] = int(facts["running_alive"])      # per task, sebanding CLI
    counts["running_tasks"] = int(facts["running_total"])
    counts["agents"] = len(agents)
    return {
        "board": info,
        "generated_at": now,
        "agents": agents,
        "counts": counts,
        "has_db": has_db,
    }


def _agent_tasks(conn, limit: int) -> list:
    """Task terbaru per status, `running` di atas. Arsip tidak pernah ikut."""
    rank = "CASE status " + " ".join("WHEN ? THEN ?" for _ in AGENT_STATUS_KEYS) + " ELSE 9 END"
    params: list = []
    for index, key in enumerate(AGENT_STATUS_KEYS):
        params += [key, index]
    sql = (
        "SELECT id, title, assignee, status, priority, created_at, started_at, completed_at, "
        "worker_pid, workspace_path, workspace_kind, branch_name, last_heartbeat_at, block_kind, "
        "substr(COALESCE(body, ''), 1, %d) AS body_preview, "
        "length(COALESCE(body, '')) AS body_len "
        "FROM tasks WHERE status <> ? ORDER BY %s, COALESCE(started_at, created_at) DESC LIMIT ?"
        % (AGENT_PREVIEW_CHARS, rank)
    )
    params.append(limit)
    return conn.execute(sql, ["archived"] + params).fetchall()


def _comment_counts(conn, ids: list) -> dict:
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT task_id, COUNT(*) AS n FROM task_comments WHERE task_id IN %s GROUP BY task_id"
        % _placeholder(ids), list(ids))
    return {str(r["task_id"]): int(r["n"]) for r in rows}


def _last_comments(conn, ids: list) -> dict:
    """Komentar terakhir per task (dipotong) — untuk daftar, bukan isi penuh."""
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT task_id, author, substr(COALESCE(body, ''), 1, %d) AS body, created_at "
        "FROM task_comments WHERE id IN "
        "(SELECT MAX(id) FROM task_comments WHERE task_id IN %s GROUP BY task_id)"
        % (AGENT_COMMENT_PREVIEW, _placeholder(ids)), list(ids))
    out = {}
    for r in rows:
        out[str(r["task_id"])] = {"author": r["author"], "body": r["body"],
                                  "created_at": r["created_at"]}
    return out


def _last_events(conn, ids: list) -> dict:
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT task_id, kind, substr(COALESCE(CAST(payload AS TEXT), ''), 1, %d) AS payload, "
        "created_at FROM task_events WHERE id IN "
        "(SELECT MAX(id) FROM task_events WHERE task_id IN %s GROUP BY task_id)"
        % (AGENT_EVENT_PREVIEW, _placeholder(ids)), list(ids))
    out = {}
    for r in rows:
        out[str(r["task_id"])] = {"kind": r["kind"], "payload_preview": r["payload"],
                                  "created_at": r["created_at"]}
    return out


def _task_link_map(conn, ids: list):
    """`(parents, children)` — masing-masing `{task_id: [id, ...]}`."""
    parents: dict = {i: [] for i in ids}
    children: dict = {i: [] for i in ids}
    if not ids:
        return parents, children
    where = _placeholder(ids)
    rows = conn.execute(
        "SELECT parent_id, child_id FROM task_links WHERE parent_id IN %s OR child_id IN %s"
        % (where, where), list(ids) + list(ids))
    for r in rows:
        parent, child = str(r["parent_id"]), str(r["child_id"])
        if child in parents:
            parents[child].append(parent)
        if parent in children:
            children[parent].append(child)
    return parents, children


def _row_field(row, key, default=None):
    """Nilai kolom kalau ada — detail memakai `SELECT *`, daftar memakai kolom terpilih."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _task_summary(row, *, comment_count: int, last_comment, last_event,
                  parents: list, children: list) -> dict:
    pid = row["worker_pid"]
    return {
        "id": row["id"],
        "title": row["title"],
        "status": row["status"],
        "assignee": row["assignee"],
        "priority": row["priority"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "worker_pid": pid,
        "worker_alive": bool(row["status"] == "running" and _pid_alive(pid)),
        "last_heartbeat_at": row["last_heartbeat_at"],
        "workspace_path": row["workspace_path"],
        "workspace_kind": row["workspace_kind"],
        "branch_name": row["branch_name"],
        "block_kind": row["block_kind"],
        "body_preview": _row_field(row, "body_preview", "") or "",
        "body_truncated": bool((_row_field(row, "body_len", 0) or 0) > AGENT_PREVIEW_CHARS),
        "comment_count": comment_count,
        "last_comment": last_comment,
        "last_event": last_event,
        "parents": parents,
        "children": children,
    }


def agent_board_payload(slug: str, limit: int) -> dict:
    """Payload `GET /api/agents/board` untuk satu board."""
    info = _board_meta(slug)
    info.pop("archived", None)
    info["current"] = bool(slug == _current_board())
    payload = {
        "board": info,
        "generated_at": int(time.time()),
        "counts": {},
        "tasks": [],
        "has_db": _board_db_path(slug).is_file(),
    }
    if not payload["has_db"]:
        payload["counts"] = {key: 0 for key in AGENT_STATUS_KEYS}
        payload["counts"]["total"] = 0
        return payload

    def work(conn):
        counts: dict = {key: 0 for key in AGENT_STATUS_KEYS}
        for row in conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"):
            counts[str(row["status"])] = int(row["n"])
        counts["total"] = sum(v for k, v in counts.items() if k != "total")

        rows = _agent_tasks(conn, limit)
        ids = [str(r["id"]) for r in rows]
        counts_by_task = _comment_counts(conn, ids)
        comments = _last_comments(conn, ids)
        events = _last_events(conn, ids)
        parents, children = _task_link_map(conn, ids)
        tasks = [
            _task_summary(
                r,
                comment_count=counts_by_task.get(str(r["id"]), 0),
                last_comment=comments.get(str(r["id"])),
                last_event=events.get(str(r["id"])),
                parents=sorted(parents.get(str(r["id"]), [])),
                children=sorted(children.get(str(r["id"]), [])),
            )
            for r in rows
        ]
        return {"counts": counts, "tasks": tasks, "returned": len(tasks)}

    payload.update(_board_query(_board_db_path(slug), work))
    counts = payload.get("counts", {})
    visible = int(counts.get("total", 0)) - int(counts.get("archived", 0))
    payload["truncated"] = bool(visible > payload.get("returned", 0))
    return payload


def agent_task_payload(slug: str, task_id: str) -> dict | None:
    """Payload `GET /api/agents/task`: satu task lengkap (body penuh + riwayat)."""
    db = _board_db_path(slug)
    if not db.is_file():
        return None

    def work(conn):
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        ids = [task_id]
        counts_by_task = _comment_counts(conn, ids)
        comments = [
            {"author": r["author"],
             "body": (r["body"] or "")[:4000],
             "created_at": r["created_at"]}
            for r in conn.execute(
                "SELECT author, body, created_at FROM task_comments WHERE task_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?", (task_id, AGENT_DETAIL_COMMENTS))
        ]
        comments.reverse()               # terlama -> terbaru, enak dibaca dari atas
        events = [
            {"kind": r["kind"],
             "payload_preview": (r["payload"] or "")[:AGENT_EVENT_PREVIEW],
             "created_at": r["created_at"]}
            for r in conn.execute(
                "SELECT kind, CAST(payload AS TEXT) AS payload, created_at FROM task_events "
                "WHERE task_id = ? ORDER BY id DESC LIMIT ?", (task_id, AGENT_DETAIL_EVENTS))
        ]
        events.reverse()
        runs = [
            {"id": r["id"], "profile": r["profile"], "status": r["status"],
             "outcome": r["outcome"], "worker_pid": r["worker_pid"],
             "started_at": r["started_at"], "ended_at": r["ended_at"],
             "summary": (r["summary"] or "")[:400]}
            for r in conn.execute(
                "SELECT id, profile, status, outcome, worker_pid, started_at, ended_at, summary "
                "FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT ?",
                (task_id, AGENT_DETAIL_RUNS))
        ]
        parents, children = _task_link_map(conn, ids)
        task = _task_summary(
            row,
            comment_count=counts_by_task.get(task_id, 0),
            last_comment=None,
            last_event=None,
            parents=sorted(parents.get(task_id, [])),
            children=sorted(children.get(task_id, [])),
        )
        body = row["body"] or ""
        task["body"] = body[:AGENT_BODY_MAX]
        task["body_truncated"] = len(body) > AGENT_BODY_MAX
        task["last_failure_error"] = row["last_failure_error"]
        task["comments"] = comments
        task["events"] = events
        task["runs"] = runs
        return task

    task = _board_query(db, work)
    if task is None:
        return None
    info = _board_meta(slug)
    info.pop("archived", None)
    return {"board": info, "generated_at": int(time.time()), "task": task}


def _sniff_image(data: bytes) -> tuple[str, str] | None:
    """Deteksi JPEG/PNG/GIF/WebP dari magic bytes → (mime, ekstensi)."""
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "jpg"
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "png"
    if len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def _image_path_allowed(resolved: Path) -> bool:
    """Path gambar hanya boleh di bawah cache webchat, cache Hermes, atau folder
    uploads/ milik webchat ini (lampiran baru disimpan di sana)."""
    for root in (WEBCHAT_IMAGES_ROOT, HERMES_IMAGES_ROOT, UPLOAD_DIR):
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _prune_old_webchat_images() -> None:
    """Bersihkan berkas webchat >14 hari — best-effort, tidak boleh gagalkan upload."""
    root = WEBCHAT_IMAGES_ROOT
    if not root.is_dir():
        return
    cutoff = time.time() - _IMAGE_RETENTION_DAYS * 24 * 3600
    try:
        for path in root.rglob("*"):
            if path.is_file():
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                except OSError:
                    pass
        for child in root.iterdir():
            if child.is_dir():
                try:
                    child.rmdir()
                except OSError:
                    pass
    except OSError:
        pass


# --- Otomasi: helper lokal (read-only) -------------------------------------

_SKILL_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def _skill_frontmatter(path: Path) -> dict:
    """Ambil name/description dari frontmatter SKILL.md — best-effort, tanpa YAML."""
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return {}
    match = _SKILL_FM_RE.match(head)
    body = match.group(1) if match else ""
    out: dict = {}
    for line in body.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        if key in ("name", "description") and key not in out:
            out[key] = value.strip().strip('"').strip("'")
    return out


def local_skill_list() -> list:
    """Daftar skill dari disk ($HERMES_HOME/skills/<kategori>/<nama>/SKILL.md).

    Dipakai HANYA sebagai cadangan ketika `GET /v1/skills` gagal (build ini
    mengembalikan 500 karena signature `_find_all_skills()` tidak cocok).
    Bentuknya sengaja sama dengan respons gateway: {name, description, category}.
    """
    root = _hermes_root() / "skills"
    items: list = []
    if not root.is_dir():
        return items
    for skill_file in sorted(root.glob("*/*/SKILL.md")):
        meta = _skill_frontmatter(skill_file)
        name = meta.get("name") or skill_file.parent.name
        items.append({
            "name": name,
            "description": meta.get("description") or "",
            "category": skill_file.parent.parent.name,
        })
    items.sort(key=lambda s: ((s.get("category") or ""), s.get("name") or ""))
    return items


def _process_uptime(pid) -> int | None:
    """Uptime proses (detik) dari /proc — None kalau pid mati/tidak terbaca."""
    try:
        pid = int(pid)
        stat = Path("/proc/%d/stat" % pid).read_text(encoding="utf-8", errors="replace")
        fields = stat[stat.rindex(")") + 2:].split()
        start_ticks = int(fields[19])              # field 22 (starttime), 0-based 19
        hz = os.sysconf("SC_CLK_TCK") or 100
        uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
        return max(0, int(uptime - start_ticks / hz))
    except (OSError, ValueError, IndexError):
        return None


def _kanban_health() -> dict:
    """Ringkas kondisi penyimpanan kanban: jumlah board + ukuran db-nya.

    Board `default` tidak punya direktori (db-nya `$HERMES_HOME/kanban.db`), jadi
    hitungannya memakai `list_board_meta()` — bukan menghitung folder, yang akan
    melaporkan 0 board padahal papan default ada.
    """
    db_bytes = 0
    boards = 0
    try:
        metas = list_board_meta()
        boards = len(metas)
        for meta in metas:
            try:
                db_bytes += _board_db_path(meta["slug"]).stat().st_size
            except OSError:
                continue
    except Exception:  # noqa: BLE001 - panel status tidak boleh ikut mati
        pass
    boards_dir = _boards_root()
    return {"boards": boards, "db_bytes": db_bytes, "root": str(boards_dir),
            "ok": boards_dir.is_dir() or (_hermes_root() / "kanban.db").is_file()}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "hermes-webchat/1.0"

    # -- helpers ---------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature
        sys.stderr.write("[webchat] %s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, status: int, payload, extra_headers: list | None = None) -> None:
        # `payload` boleh dict ATAU list (mis. `/api/agents/boards` mengembalikan
        # daftar board langsung).
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
        if path.startswith("/api/") or path.startswith("/v1/") or path.startswith("/health/"):
            # POST berisi body yang belum dibaca (mis. upload 4 gambar tanpa sesi) →
            # tutup koneksi agar sisa byte tidak jadi "request" hantu di keep-alive.
            self.close_connection = True
            self._fail(401, "login required", "Buka /login lalu masukkan kredensial.",
                       extra_headers=[("Connection", "close")])
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
        # Pemantau Agent (/agents + API JSON-nya). Endpoint ini LOKAL — membaca
        # kanban.db sendiri, bukan diteruskan ke gateway — jadi tetap wajib sesi
        # login (di atas) plus cek same-origin seperti jalur POST.
        if path in ("/agents", "/agents/"):
            if not self._same_origin():
                return self._fail(403, "cross-origin ditolak")
            return self._serve_static("agents.html")
        # Workspace — kantor virtual realtime; datanya dari /api/agents/live.
        if path in ("/workspace", "/workspace/"):
            if not self._same_origin():
                return self._fail(403, "cross-origin ditolak")
            return self._serve_static("workspace.html")
        # Otomasi — cron, skills, toolsets, status sistem.
        if path in ("/otomasi", "/otomasi/"):
            if not self._same_origin():
                return self._fail(403, "cross-origin ditolak")
            return self._serve_static("otomasi.html")
        if path == "/api/image":
            if not self._same_origin():
                return self._fail(403, "cross-origin ditolak")
            return self._serve_cached_image()
        # Lampiran baru (gambar & dokumen) disajikan lewat id pendek.
        if path.startswith("/api/uploads/"):
            return self._serve_upload(path[len("/api/uploads/"):])
        if path in ("/api/agents/boards", "/api/agents/board", "/api/agents/task",
                    "/api/agents/live", "/api/agents/assignees",
                    "/api/agents/dispatch-preview"):
            if not self._same_origin():
                return self._fail(403, "cross-origin ditolak")
            return self._api_agents(path)
        if path == "/api/models":
            return self._proxy_models()
        # --- Otomasi (read-only) ---------------------------------------
        # Panel status menyatukan dua endpoint gateway + fakta lokal (uptime,
        # kanban, db) yang tidak dikirim gateway mana pun.
        if path == "/api/status":
            return self._api_status()
        if path in ("/health/detailed", "/v1/capabilities", "/v1/toolsets"):
            return self._proxy_get()
        if path == "/v1/skills":
            return self._proxy_skills()
        if path == "/api/jobs" or JOB_ITEM_RE.match(path):
            return self._proxy_get()
        job_output = re.match(r"^/api/jobs/([a-f0-9]{12})/output$", path)
        if job_output:
            return self._job_output(job_output.group(1))
        if path.startswith("/v1/artifacts/download/"):
            if not ARTIFACT_DL_RE.match(path):
                return self._fail(400, "id artifact tidak valid",
                                  "Id artifact hanya huruf/angka/._- (maks 128).")
            return self._proxy_artifact()
        # Hermes-specific read-only surface (curated model inventory + session history),
        # proxied so the browser can reach it same-origin without ever holding the key.
        if path == "/api/model/options" or path == "/api/sessions" or path.startswith("/api/sessions/"):
            if path.startswith("/api/sessions/"):
                sid = path[len("/api/sessions/"):].split("/")[0]
                if not SESSION_ID_RE.match(sid):
                    return self._fail(400, "id sesi tidak valid", sid[:80])
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
            self.close_connection = True
            return
        if not self._same_origin():
            # Body POST belum dibaca → sisa byte akan diurai sebagai request baru
            # pada koneksi keep-alive (400 "Bad request syntax"). Tutup koneksinya.
            self.close_connection = True
            return self._fail(403, "cross-origin ditolak", extra_headers=[("Connection", "close")])
        if path == "/api/upload":
            return self._do_upload()
        # Papan kanban: mutasi task/board (lewat CLI `hermes kanban`).
        if path in ("/api/agents/task-create", "/api/agents/task-update",
                    "/api/agents/board-create"):
            return self._api_agents_post(path)
        if path == "/v1/chat/completions":
            return self._proxy_chat()
        # --- Otomasi: mutasi cron/sesi/artifact -------------------------
        if path == "/v1/artifacts/upload":
            return self._upload_artifact()
        if path in ("/api/jobs", "/api/cron/fire") or JOB_MUTATION_RE.match(path):
            if not self._mutation_allowed():
                return
            # pause/resume/run boleh tanpa body; create wajib JSON objek.
            allow_empty = bool(JOB_MUTATION_RE.match(path))
            return self._proxy_json("POST", require_json=path == "/api/jobs",
                                    allow_empty=allow_empty)
        if path == "/api/sessions":
            if not self._mutation_allowed():
                return
            return self._proxy_json("POST", require_json=True)
        if SESSION_ACTION_RE.match(path):
            if not self._mutation_allowed():
                return
            return self._proxy_json("POST", require_json=True)
        # Kontrol run: buat run, jawab approval, stop, steer. Semuanya JSON pendek,
        # bukan stream. Inilah yang membuat kartu konfirmasi di UI bisa dijawab.
        if path == "/v1/runs" or path.startswith("/v1/runs/"):
            return self._proxy_post_json()
        self._fail(404, "not found", path)

    def do_PATCH(self) -> None:  # noqa: N802 - stdlib signature
        """PATCH — hanya untuk metadata sesi dan job cron (whitelist ketat)."""
        path = urllib.parse.urlsplit(self.path).path
        if not self._require_auth():
            self.close_connection = True
            return
        if not self._same_origin():
            self.close_connection = True
            return self._fail(403, "cross-origin ditolak", extra_headers=[("Connection", "close")])
        if JOB_ITEM_RE.match(path) or (SESSION_ITEM_RE.match(path) and
                                       path.startswith("/api/sessions/")):
            if not self._mutation_allowed():
                return
            return self._proxy_json("PATCH", require_json=True)
        return self._fail(404, "not found", path)

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib signature
        """DELETE — hapus sesi atau job cron; tidak ada body yang dibaca."""
        path = urllib.parse.urlsplit(self.path).path
        if not self._require_auth():
            self.close_connection = True
            return
        if not self._same_origin():
            self.close_connection = True
            return self._fail(403, "cross-origin ditolak", extra_headers=[("Connection", "close")])
        if JOB_ITEM_RE.match(path) or (SESSION_ITEM_RE.match(path) and
                                       path.startswith("/api/sessions/")):
            if not self._mutation_allowed():
                return
            return self._proxy_json("DELETE", require_json=False)
        return self._fail(404, "not found", path)

    def _mutation_allowed(self) -> bool:
        """Rate limit mutasi bersama kanban/sesi/cron. False = respons sudah dikirim."""
        key = "%s|%s" % (self.address_string(), self._user() or "-")
        if mutate_allowed(key):
            return True
        self._fail(429, "terlalu banyak perubahan",
                   "Maksimal %d aksi per %d detik. Coba lagi sebentar."
                   % (AGENT_MUTATE_LIMIT, AGENT_MUTATE_WINDOW))
        return False

    # -- login / logout --------------------------------------------------

    def _read_body(self, limit: int = 64 * 1024) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        if length <= 0 or length > limit:
            return b""
        return self.rfile.read(length)

    def _read_body_exact(self) -> bytes | None:
        """Baca body persis Content-Length; None kalau header tidak valid."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length < 0:
            return None
        return self.rfile.read(length)

    def _do_upload(self) -> None:
        """Satu jalur unggah untuk gambar DAN dokumen.

        Kontrak respons memuat field lama (`path`, `url`, `name`, `bytes`, `mime`)
        sekaligus field baru (`id`, `kind`, `text`, `chars`, `truncated`, `note`),
        jadi transkrip/klien lama tetap jalan.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            return self._fail(400, "bad Content-Length", extra_headers=[("Connection", "close")])
        if length <= 0:
            return self._fail(400, "body kosong", "Tidak ada isi yang dikirim.")
        if length > MAX_UPLOAD_BYTES + _IMAGE_UPLOAD_SLACK:
            # Tolak TANPA membaca body → wajib tutup koneksi supaya sisa byte
            # tidak diurai sebagai request berikutnya pada keep-alive.
            self.close_connection = True
            return self._fail(413, "file terlalu besar",
                              "Maksimal %d MB per berkas." % MAX_UPLOAD_MB,
                              extra_headers=[("Connection", "close")])
        body = self._read_body_exact()
        if body is None:
            return self._fail(400, "bad Content-Length")
        if len(body) != length:
            return self._fail(400, "body tidak lengkap")

        ctype = (self.headers.get("Content-Type") or "").strip()
        ctype_main = ctype.split(";")[0].strip().lower()
        file_data = b""
        orig_name = "file"
        part_type = ""
        if ctype_main == "multipart/form-data":
            parsed = parse_multipart(body, ctype)
            if parsed is None:
                return self._fail(400, "boundary multipart tidak ditemukan")
            _fields, files = parsed
            picked = next((f for f in files if f.get("field") == "file"),
                          files[0] if files else None)
            if picked is None:
                return self._fail(400, "file tidak ditemukan",
                                  "Sertakan satu file pada field `file`.")
            file_data = picked.get("data") or b""
            orig_name = picked.get("filename") or "file"
            part_type = (picked.get("type") or "").split(";")[0].strip().lower()
            if not orig_name:
                orig_name = "file" + (mimetypes.guess_extension(part_type) or "")
        else:
            # Klien lama boleh mengirim body mentah (mis. image/png langsung).
            file_data = body
            orig_name = "file" + (mimetypes.guess_extension(ctype_main) or "")

        if not file_data:
            return self._fail(400, "file kosong")
        if len(file_data) > MAX_UPLOAD_BYTES:
            return self._fail(413, "file terlalu besar", "Maksimal %d MB per berkas." % MAX_UPLOAD_MB)

        name = safe_filename(orig_name)
        ext = os.path.splitext(name)[1].lower()
        if not ext:
            # Nama tanpa ekstensi (path dibuang) → tebak dari Content-Type part.
            guess = part_type or (ctype_main if ctype_main not in ("", "multipart/form-data") else "")
            ext = mimetypes.guess_extension(guess) or ""
            if ext == ".jpe":
                ext = ".jpg"
            name = (name or "file") + ext

        # Gambar: percayai isi berkas, bukan ekstensi (pertahanan dari file
        # berbahaya yang menyamar). Dokumen tetap divalidasi dari ekstensi.
        sniffed = _sniff_image(file_data)
        if ext in IMAGE_EXT:
            if sniffed is None:
                return self._fail(415, "isi berkas bukan gambar yang didukung",
                                  "Hanya JPEG, PNG, GIF, dan WebP (dicek dari isi berkas).")
            ext = "." + sniffed[1]
            name = os.path.splitext(name)[0] + ext
        elif sniffed is not None and ext not in ALLOWED_EXT:
            ext = "." + sniffed[1]
            name = os.path.splitext(name)[0] + ext

        if ext not in ALLOWED_EXT:
            return self._fail(
                415, "tipe file tidak didukung",
                "Ekstensi %r tidak didukung. Boleh: %s." % (
                    ext or "(tanpa ekstensi)",
                    ", ".join(sorted(e.lstrip(".") for e in ALLOWED_EXT))),
            )

        day = time.strftime("%Y-%m-%d", time.gmtime())
        folder = UPLOAD_DIR / day
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.chmod(UPLOAD_DIR, 0o700)
        except OSError as exc:
            return self._fail(500, "tidak bisa membuat folder unggahan", str(exc))

        for _ in range(5):
            ident = uuid.uuid4().hex[:8]
            target = (folder / (ident + "_" + name)).resolve()
            if UPLOAD_DIR not in target.parents:
                return self._fail(400, "nama file tidak valid")
            try:
                # O_EXCL + mode 600 sejak create: berkas tidak pernah terlanjur 644.
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    os.write(fd, file_data)
                finally:
                    os.close(fd)
                break
            except FileExistsError:
                continue
            except OSError as exc:
                return self._fail(500, "gagal menyimpan file", str(exc))
        else:
            return self._fail(500, "gagal menyimpan file", "nama bentrok berulang")

        record = build_upload_record(ident, name, target, len(file_data))
        if record["kind"] == "image":
            try:
                _prune_old_webchat_images()   # bersihkan cache gambar lama (14 hari)
            except Exception:  # noqa: BLE001 - pembersihan tidak boleh menggagalkan upload
                pass
        record["ok"] = True
        self._json(200, record)

    def _serve_upload(self, ident: str) -> None:
        """Sajikan lampiran (dipakai <img> untuk thumbnail setelah reload).

        Hanya id 8 hex, hanya file di dalam UPLOAD_DIR (path kanonik diperiksa),
        dan hanya ekstensi yang diizinkan — jadi `../` tidak bisa keluar folder.
        """
        ident = urllib.parse.unquote(ident or "").strip()
        if not re.fullmatch(r"[0-9a-f]{8}", ident):
            return self._fail(400, "id unggahan tidak valid")
        target = None
        try:
            days = sorted((p for p in UPLOAD_DIR.iterdir() if p.is_dir()), reverse=True)
        except OSError:
            days = []
        for day in days:
            for candidate in day.glob(ident + "_*"):
                if candidate.is_file():
                    target = candidate.resolve()
                    break
            if target:
                break
        if target is None:
            return self._fail(404, "file tidak ditemukan")
        if UPLOAD_DIR not in target.parents:
            return self._fail(403, "akses di luar folder unggahan")
        if target.suffix.lower() not in ALLOWED_EXT:
            return self._fail(415, "tipe file tidak disajikan")
        try:
            data = target.read_bytes()
        except OSError as exc:
            return self._fail(500, "gagal membaca file", str(exc))
        mime = _EXT_MIME.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0] \
            or "application/octet-stream"
        quoted = urllib.parse.quote(target.name.split("_", 1)[-1])
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        self.send_header("Content-Disposition", "inline; filename*=UTF-8''" + quoted)
        self.end_headers()
        self.wfile.write(data)

    def _serve_cached_image(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        raw = (query.get("path") or [""])[0].strip()
        if not raw:
            return self._fail(400, "parameter path wajib")
        try:
            resolved = Path(raw).resolve()
        except OSError:
            return self._fail(400, "path tidak valid")
        if not _image_path_allowed(resolved):
            return self._fail(403, "path ditolak")
        try:
            data = resolved.read_bytes()
        except OSError:
            return self._fail(404, "berkas tidak ditemukan")
        sniffed = _sniff_image(data)
        if sniffed is None:
            return self._fail(415, "bukan gambar yang didukung")
        mime = sniffed[0]
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

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

    # -- pemantau agent (kanban.db, read-only) ---------------------------

    def _api_agents(self, path: str) -> None:
        """`GET /api/agents/…` — data papan kanban Hermes untuk halaman /agents.

        Read-only dan lokal (tidak di-proxy): slug divalidasi regex sebelum
        menyentuh filesystem, jadi tidak ada jalur untuk keluar dari direktori
        boards. `limit` dibatasi 1–200.
        """
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        slug = (query.get("slug") or [""])[0].strip().lower()
        bad_slug = self._board_slug_error(slug)

        def check_slug() -> bool:
            """True kalau boleh lanjut; kalau tidak, respons sudah dikirim."""
            if bad_slug:
                self._fail(400, "slug board tidak valid", bad_slug)
                return False
            if not _board_exists(slug):
                self._fail(404, "board tidak ditemukan", slug)
                return False
            return True

        try:
            if path == "/api/agents/boards":
                current = _current_board()
                boards = []
                for meta in list_board_meta():
                    counts = _status_counts(meta["slug"])
                    boards.append({
                        "slug": meta["slug"],
                        "name": meta["name"],
                        "description": meta.get("description") or "",
                        "task_count": sum(counts.values()),
                        "running_count": int(counts.get("running", 0)),
                        "current": bool(meta["slug"] == current),
                    })
                return self._json(200, boards)

            if path == "/api/agents/board":
                if not check_slug():
                    return None
                payload = agent_board_payload(slug, _agent_limit(query))
                return self._json(200, payload)

            if path == "/api/agents/task":
                task_id = (query.get("id") or [""])[0].strip()
                if not check_slug():
                    return None
                if not TASK_ID_RE.match(task_id):
                    return self._fail(400, "id task tidak valid",
                                      "Contoh yang benar: t_1a2b3c4d")
                payload = agent_task_payload(slug, task_id)
                if payload is None:
                    return self._fail(404, "task tidak ditemukan", task_id)
                return self._json(200, payload)

            if path == "/api/agents/live":
                # Kantor virtual: state tiap profil untuk satu board. Aman
                # di-polling tiap 3 detik — hanya kolom ringkas, tanpa body task.
                if not check_slug():
                    return None
                return self._json(200, agent_live_payload(slug))

            if path == "/api/agents/assignees":
                # Daftar profil yang boleh jadi assignee. Dipakai modal "Task baru"
                # dan validasi server; cache 30 dtk karena CLI butuh ~4 dtk per panggilan.
                items = kanban_assignee_items()
                names = [str(i.get("name")) for i in items]
                note = ""
                if len(names) == 1:
                    note = ("Baru ada satu profil (%s). Worker dijalankan oleh profil itu; "
                            "tambah profil dengan `hermes profile create <nama>`." % names[0])
                return self._json(200, {"assignees": items, "count": len(names),
                                        "single": len(names) == 1, "note": note,
                                        "generated_at": int(time.time())})

            if path == "/api/agents/dispatch-preview":
                # Pratinjau SATU pass dispatcher (dry-run). Tidak men-spawn worker:
                # eksekusi nyata tetap milik dispatcher gateway.
                if not check_slug():
                    return None
                preview = kanban_json(["--board", slug, "dispatch", "--dry-run", "--json"])
                return self._json(200, {"board": slug, "generated_at": int(time.time()),
                                        "preview": preview})
        except KanbanError as exc:
            return self._fail(exc.status, exc.message, exc.detail)
        except ValueError as exc:
            return self._fail(400, "permintaan tidak valid", str(exc))
        except Exception as exc:  # noqa: BLE001 - jangan biarkan UI kosong tanpa sebab
            return self._fail(500, "gagal membaca papan kanban",
                              "%s: %s" % (type(exc).__name__, exc))
        return self._fail(404, "not found", path)

    @staticmethod
    def _board_slug_error(slug: str) -> str:
        """Pesan kesalahan slug, atau string kosong kalau sah."""
        return board_slug_problem(slug)

    # -- papan kanban: mutasi (POST) -------------------------------------

    def _json_body(self) -> dict:
        """Baca body JSON dengan batas ukuran. Gagal → KanbanError 400/413."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = -1
        if length <= 0:
            raise KanbanError(400, "body JSON wajib diisi",
                              "Kirim objek JSON, mis. {\"title\": \"...\"}.")
        if length > AGENT_JSON_MAX:
            self.close_connection = True
            raise KanbanError(413, "body terlalu besar",
                              "Maksimal %d KB." % (AGENT_JSON_MAX // 1024))
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise KanbanError(400, "body bukan JSON yang valid",
                              "Content-Type harus application/json dan body harus JSON.") from None
        if not isinstance(data, dict):
            raise KanbanError(400, "body JSON harus berupa objek",
                              "Array/angka/string tidak diterima.")
        return data

    def _api_agents_post(self, path: str) -> None:
        """POST /api/agents/{task-create,task-update,board-create}.

        Autentikasi + cek same-origin sudah dilakukan do_POST sebelum ke sini.
        Semua mutasi lewat CLI `hermes kanban` (lihat kanban_run): tidak ada
        penulisan langsung ke kanban.db dari handler ini.
        """
        try:
            data = self._json_body()
            key = "%s|%s" % (self.address_string(), self._user() or "-")
            if not mutate_allowed(key):
                raise KanbanError(429, "terlalu banyak perubahan",
                                  "Maksimal %d aksi per %d detik. Coba lagi sebentar."
                                  % (AGENT_MUTATE_LIMIT, AGENT_MUTATE_WINDOW))

            if path == "/api/agents/board-create":
                result = kanban_board_create(data)
                return self._json(201, {"ok": True, "action": "board-create",
                                        "board": result["board"],
                                        "message": result["message"]})

            slug = str(data.get("slug") or "").strip().lower() or _current_board()
            problem = board_slug_problem(slug)
            if problem:
                raise KanbanError(400, "slug board tidak valid", problem)
            if not _board_exists(slug):
                raise KanbanError(404, "board tidak ditemukan", slug)
            if _board_meta(slug).get("archived"):
                raise KanbanError(404, "board sudah diarsipkan", slug)

            if path == "/api/agents/task-create":
                task = kanban_create(slug, data)
                task_id = str(task.get("id") or "")
                return self._json(201, {
                    "ok": True, "action": "create", "board": slug, "id": task_id,
                    "task": task, "status": task.get("status"),
                    "message": "Task %s dibuat (status %s)." % (task_id, task.get("status")),
                })

            if path == "/api/agents/task-update":
                action = str(data.get("action") or "").strip()
                if action not in KANBAN_ACTIONS:
                    raise KanbanError(400, "action tidak dikenal",
                                      "Pilih salah satu: " + ", ".join(KANBAN_ACTIONS))
                task_id = str(data.get("id") or "").strip()
                if not TASK_ID_RE.match(task_id):
                    raise KanbanError(400, "id task tidak valid",
                                      "Contoh yang benar: t_1a2b3c4d")
                before = agent_task_payload(slug, task_id)
                if before is None:
                    raise KanbanError(404, "task tidak ditemukan", task_id)
                result = kanban_mutate(slug, action, task_id, data)
                after = agent_task_payload(slug, task_id)
                task = (after or {}).get("task") if after else None
                return self._json(200, {
                    "ok": True, "action": action, "board": slug, "id": task_id,
                    "task": task, "status": (task or {}).get("status"),
                    "previous_status": ((before or {}).get("task") or {}).get("status"),
                    "message": result["message"] or "Aksi %s berhasil." % action,
                })

            raise KanbanError(404, "not found", path)
        except KanbanError as exc:
            return self._fail(exc.status, exc.message, exc.detail)
        except Exception as exc:  # noqa: BLE001 - jangan biarkan handler menggantung
            return self._fail(500, "gagal menjalankan aksi kanban",
                              "%s: %s" % (type(exc).__name__, _kanban_scrub(str(exc))))

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

    def _proxy_json(self, method: str, *, require_json: bool = True,
                    allow_empty: bool = False, timeout: float = 60) -> None:
        """Passthrough JSON untuk method apa pun (POST/PATCH/DELETE).

        Body divalidasi LOKAL lebih dulu: bukan JSON objek → 400 tanpa menyentuh
        gateway. Ini yang membuat uji negatif `body bukan JSON` konsisten.
        Jalur `/v1/runs` tetap memakai `_proxy_post_json` (tidak diubah).
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._fail(400, "bad Content-Length")
        if length < 0 or length > OTOMASI_JSON_MAX:
            self.close_connection = True
            return self._fail(413, "body terlalu besar",
                              "Maksimal %d KB." % (OTOMASI_JSON_MAX // 1024))
        body = b""
        if length:
            body = self.rfile.read(length)
            try:
                parsed = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return self._fail(400, "body bukan JSON yang valid",
                                  "Content-Type harus application/json dan body harus JSON.")
            if not isinstance(parsed, dict):
                return self._fail(400, "body JSON harus berupa objek",
                                  "Array/angka/string tidak diterima.")
        elif require_json and not allow_empty:
            return self._fail(400, "body JSON wajib diisi",
                              "Kirim objek JSON, mis. {\"name\": \"...\"}.")

        req = urllib.request.Request(
            UPSTREAM + self.path, data=(body or None), method=method,
            headers=self._upstream_headers(stream=False),
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
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

    def _proxy_skills(self) -> None:
        """`GET /v1/skills` + cadangan lokal kalau endpoint gateway rusak.

        Build terpasang (0.22.1) memanggil `_find_all_skills(include_editorial=…)`
        padahal signature-nya belum punya kwarg itu → selalu 500. Daripada panel
        Skills kosong, daftar dibaca dari disk dengan bentuk JSON yang sama dan
        ditandai `source: "local-fallback"` supaya jujur soal asalnya.
        """
        req = urllib.request.Request(UPSTREAM + "/v1/skills",
                                     headers=self._upstream_headers(stream=False))
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return self._upstream_error(exc)
            return self._skills_fallback("gateway HTTP %d" % exc.code)
        except Exception as exc:  # noqa: BLE001 - gateway mati → tetap tampilkan daftar
            return self._skills_fallback(str(exc))
        if status != 200:
            return self._skills_fallback("gateway HTTP %d" % status)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._skills_fallback("respons gateway bukan JSON")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _skills_fallback(self, reason: str) -> None:
        items = local_skill_list()
        self._json(200, {
            "object": "list", "data": items, "count": len(items),
            "source": "local-fallback",
            "note": "GET /v1/skills gagal (%s); daftar dibaca dari $HERMES_HOME/skills."
                    % reason,
        })

    def _proxy_artifact(self) -> None:
        """`GET /v1/artifacts/download/{id}` — biner apa adanya + header unduhan.

        Artifact gateway bersifat one-shot (unduhan kedua 404), jadi handler ini
        tidak boleh di-prefetch: tautan di UI hanya diambil saat diklik.
        """
        req = urllib.request.Request(UPSTREAM + self.path,
                                     headers=self._upstream_headers(stream=False))
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
                ctype = resp.headers.get("Content-Type") or "application/octet-stream"
                disposition = resp.headers.get("Content-Disposition") or ""
                sha = resp.headers.get("X-Artifact-Sha256") or ""
                artifact_id = resp.headers.get("X-Artifact-Id") or ""
        except urllib.error.HTTPError as exc:
            # Tautan ini dibuka sebagai navigasi browser, jadi balas teks biasa —
            # JSON bertingkat akan tampil sebagai blob mentah di tab baru.
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except Exception:  # noqa: BLE001
                detail = ""
            if "artifact_not_found" in detail or "unknown artifact" in detail:
                msg = ("Artifact tidak tersedia. Gateway menyimpan artifact sekali pakai "
                       "dan hanya ±5 menit; minta agen mengunggah ulang.")
            elif "browser_control_disabled" in detail:
                msg = ("Artifact dimatikan di gateway (browser.extension_control.enabled "
                       "= false). Nyalakan flag itu di config Hermes untuk memakai fitur ini.")
            elif exc.code == 404:
                msg = ("Artifact tidak dikenal, sudah dipakai, atau fitur artifact dimatikan "
                       "di gateway (browser.extension_control.enabled = false).")
            elif exc.code == 410:
                msg = "Artifact sudah kedaluwarsa (410)."
            else:
                msg = "Gagal mengunduh artifact (HTTP %d)." % exc.code
            body = (msg + "\n\n" + detail).encode("utf-8")
            self.send_response(exc.code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        except Exception as exc:  # noqa: BLE001 - dilaporkan ke UI
            return self._upstream_error(exc)
        headers = [("Content-Disposition", disposition or "attachment"),
                   ("Cache-Control", "no-store")]
        if sha:
            headers.append(("X-Artifact-Sha256", sha))
        if artifact_id:
            headers.append(("X-Artifact-Id", artifact_id))
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _upload_artifact(self) -> None:
        """`POST /v1/artifacts/upload` — badan mentah diteruskan apa adanya.

        Content-Type dari klien WAJIB ikut (gateway menolak 415 kalau tidak ada di
        allowlist-nya), dan nama berkas lewat `X-Artifact-Filename`.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._fail(400, "bad Content-Length")
        if length <= 0:
            return self._fail(400, "body artifact kosong")
        if length > ARTIFACT_MAX_BYTES:
            self.close_connection = True
            return self._fail(413, "artifact terlalu besar",
                              "Maksimal %d MB." % (ARTIFACT_MAX_BYTES // (1024 * 1024)))
        if not self._mutation_allowed():
            self.close_connection = True
            return
        data = self.rfile.read(length)
        headers = self._upstream_headers(stream=False)
        headers["Content-Type"] = self.headers.get("Content-Type") or "application/octet-stream"
        filename = (self.headers.get("X-Artifact-Filename") or "").strip()
        if filename:
            headers["X-Artifact-Filename"] = filename[:200]
        req = urllib.request.Request(UPSTREAM + "/v1/artifacts/upload", data=data,
                                     method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
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

    def _api_status(self) -> None:
        """Panel diagnosa: /health/detailed + /v1/capabilities + fakta lokal.

        Gateway tidak mengirim uptime di mana pun, dan status kanban/DB hanya
        terlihat dari host ini — jadi keduanya dihitung di sini, bukan dikarang.
        """
        out: dict = {"generated_at": int(time.time()), "webchat": {
            "uptime_seconds": int(time.time() - STARTED_AT),
            "started_at": int(STARTED_AT), "port": LISTEN_PORT,
            "upstream": UPSTREAM, "python": sys.version.split()[0],
        }}
        errors = {}
        for key, path, timeout in (("gateway", "/health/detailed", 20),
                                   ("capabilities", "/v1/capabilities", 20)):
            req = urllib.request.Request(UPSTREAM + path,
                                         headers=self._upstream_headers(stream=False))
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    out[key] = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                errors[key] = "HTTP %d" % exc.code
            except Exception as exc:  # noqa: BLE001 - tampil di panel
                errors[key] = "%s: %s" % (type(exc).__name__, exc)
        gw = out.get("gateway") or {}
        out["uptime_seconds"] = _process_uptime(gw.get("pid"))
        out["kanban"] = _kanban_health()
        db_path = _hermes_root() / "state.db"
        try:
            size = db_path.stat().st_size
        except OSError:
            size = 0
        out["state_db"] = {"path": str(db_path), "bytes": size,
                           "ok": db_path.is_file() and size > 0}
        out["errors"] = errors
        self._json(200, out)

    def _job_output(self, job_id: str) -> None:
        """`GET /api/jobs/{id}/output` — hasil run terakhir dari cron/output.

        Gateway tidak mengirim badan hasil di `/api/jobs`, padahal justru itu yang
        dicari saat sebuah job terjadwal selesai. Berkasnya sudah ada di
        `$HERMES_HOME/cron/output/<job_id>/*.md`; dibaca read-only, id divalidasi
        12 hex sehingga tidak bisa keluar dari direktori itu.
        """
        out_dir = _hermes_root() / "cron" / "output" / job_id
        try:
            files = sorted((p for p in out_dir.glob("*.md") if p.is_file()),
                           key=lambda p: p.name, reverse=True)
        except OSError:
            files = []
        if not files:
            return self._json(200, {"job_id": job_id, "file": "", "text": "",
                                    "files": 0, "note": "belum ada hasil run tersimpan"})
        latest = files[0]
        try:
            text = latest.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return self._fail(500, "gagal membaca hasil run", str(exc))
        # Ambil bagian "## Response" kalau ada — itu jawaban agen, bukan prompt.
        marker = "## Response"
        if marker in text:
            text = text.split(marker, 1)[1].lstrip("\n")
        self._json(200, {"job_id": job_id, "file": latest.name,
                         "text": text[:20000], "files": len(files),
                         "truncated": len(text) > 20000})

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
