#!/usr/bin/env python3
"""
Remote-Access Portal
====================

A small self-contained web portal (Python standard library only) that sits in
front of a CortenDesk / RustDesk remote-desktop server.

Features
--------
* Landing page with email + password  **login** and **registration**.
* Accounts stored in **SQLite**.
* Two roles: **admin** and **user**.  The first account created becomes admin.
* Per-user **remote access** flag that an admin can allow / deny.
* Users connect to each other's desktops through the CortenDesk web client
  ("anyone to anyone"), gated by the remote-access flag and online status.
* **Admin control panel** listing every user with live online/offline status,
  role, remote-access toggle, and delete.
* **File manager** (admin): browse the current directory, upload / download
  files, and download the whole folder as a ZIP archive.

Run
---
    python3 app.py
    # then open http://localhost:8000

Configuration (environment variables, all optional)
----------------------------------------------------
    PORT                 listen port                    (default 8000)
    HOST                 bind address                   (default 0.0.0.0)
    DB_PATH              SQLite file                     (default ./portal.db)
    FILES_ROOT           sandbox root for file manager   (default .. — repo root)
    SECRET_KEY           cookie signing key              (default random per run)
    CORTENDESK_URL       base URL of the CortenDesk web  (default http://localhost:8080)
    CONNECT_URL_TEMPLATE how a connect link is built.    (default {base}/#/connect/{id})
                         {base} = CORTENDESK_URL, {id} = target RustDesk ID
    ALLOW_REGISTRATION   open self-service registration  (default off; an empty
                         user table always allows it, so the first admin can
                         still be created)
"""

import base64
import hashlib
import hmac
import html
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import time
import zipfile
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

try:
    import jwt as _jwt
    from cryptography.hazmat.primitives import serialization as _ser
    from cryptography.hazmat.primitives.asymmetric import rsa as _rsa
    _HAS_JWT = True
except Exception:  # pragma: no cover - OIDC disabled if libs missing
    _HAS_JWT = False

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8000"))
HOST = os.environ.get("HOST", "0.0.0.0")
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "portal.db"))
FILES_ROOT = os.path.realpath(os.environ.get("FILES_ROOT", os.path.dirname(BASE_DIR) or BASE_DIR))
# Подпись корня в крошках файлового менеджера.
ROOT_LABEL = os.path.basename(FILES_ROOT) or FILES_ROOT
MULTIPART_CHUNK = 256 * 1024   # read size while streaming an upload
SPOOL_MAX = 8 * 1024 * 1024    # keep a part in RAM up to this, then spill to disk
SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_hex(32)).encode()
CORTENDESK_URL = os.environ.get("CORTENDESK_URL", "http://localhost:8080").rstrip("/")
CONNECT_URL_TEMPLATE = os.environ.get("CONNECT_URL_TEMPLATE", "{base}/#/connect/{id}")

# Развёртывание агента RustDesk на управляемой машине (кнопка «Моё устройство»).
# Хост по умолчанию берётся из CORTENDESK_URL — это тот же сервер, что раздаёт
# веб-клиент, и отдельная переменная нужна лишь когда hbbs вынесен на другой.
RUSTDESK_HOST = os.environ.get("RUSTDESK_HOST", "") or (urlparse(CORTENDESK_URL).hostname or "")
RUSTDESK_VERSION = os.environ.get("RUSTDESK_VERSION", "1.4.9")
# Публичный ключ сервера (id_ed25519.pub). Он не секрет — его получает каждый
# клиент, — но в коде ему не место: сменится ключ, сменится и файл.
RUSTDESK_KEY_FILE = os.environ.get("RUSTDESK_KEY_FILE", os.path.join(BASE_DIR, ".rustdesk_key"))

ALLOW_REGISTRATION = os.environ.get("ALLOW_REGISTRATION", "").lower() in ("1", "true", "yes")

# Правила для email и пароля общие у самостоятельной регистрации и у формы
# создания пользователя в админке: два набора правил для одного поля
# разъезжаются при первой же правке.
EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
MIN_PASSWORD_LEN = 8

ONLINE_WINDOW = 60          # seconds since last heartbeat to count as "online"
SESSION_TTL = 7 * 24 * 3600  # cookie lifetime
CSRF_TTL = 12 * 3600        # form token lifetime; shorter than the session on purpose
MAX_BODY = 64 * 1024                    # ceiling for an ordinary POST body
MAX_UPLOAD_BODY = 2 * 1024 * 1024 * 1024  # ceiling for /files/upload only
SOCKET_TIMEOUT = 60                     # per-connection, blunts slowloris

#: Маршруты, которым позволен большой запрос. Всё остальное режется по
#: MAX_BODY ещё до чтения тела — иначе анонимный POST на несуществующий
#: путь мог набить память или /tmp.
LARGE_BODY_ROUTES = frozenset({"/files/upload"})

MULTIPART_SCAN_MAX = 1024 * 1024        # ceiling while scanning for a boundary
PBKDF_ITERS = 200_000

# --- OIDC / SSO (portal acts as the OpenID Connect identity provider) --------
OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "").rstrip("/")
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")
OIDC_REDIRECT_URIS = [u.strip() for u in os.environ.get("OIDC_REDIRECT_URIS", "").split(",") if u.strip()]
OIDC_KEY_PATH = os.environ.get("OIDC_KEY_PATH", os.path.join(BASE_DIR, "oidc_rsa_key.pem"))
OIDC_KID = "portal-key-1"
OIDC_ENABLED = _HAS_JWT and bool(OIDC_ISSUER and OIDC_CLIENT_ID and OIDC_CLIENT_SECRET)

os.makedirs(FILES_ROOT, exist_ok=True)

# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with db() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                email            TEXT UNIQUE NOT NULL,
                pw_hash          TEXT NOT NULL,
                pw_salt          TEXT NOT NULL,
                role             TEXT NOT NULL DEFAULT 'user',
                remote_allowed   INTEGER NOT NULL DEFAULT 0,
                rustdesk_id      TEXT DEFAULT '',
                rustdesk_pw      TEXT DEFAULT '',
                created_at       INTEGER NOT NULL,
                last_seen        INTEGER NOT NULL DEFAULT 0
            )
            """
        )


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF_ITERS)
    return dk.hex(), salt.hex()


def verify_password(password: str, pw_hash: str, pw_salt: str) -> bool:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(pw_salt), PBKDF_ITERS)
    return hmac.compare_digest(dk.hex(), pw_hash)


# --------------------------------------------------------------------------- #
# Signed session cookies
# --------------------------------------------------------------------------- #
def sign(payload: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = hmac.new(SECRET_KEY, raw.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{raw}.{sig}"


def unsign(token: str) -> dict | None:
    try:
        raw, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(SECRET_KEY, raw.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return None
    pad = "=" * (-len(raw) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(raw + pad))
    except Exception:
        return None
    if data.get("exp", 0) < time.time():
        return None
    return data


LOGIN_MAX_FAILURES = 10          # неудач подряд с одного адреса
LOGIN_WINDOW = 15 * 60           # за это окно, секунд

_login_fails: dict = {}
_login_lock = threading.Lock()


def _prune_login_fails(now: float) -> None:
    """Чистка старых записей. Вызывается под _login_lock.

    Нужна, чтобы словарь не рос без границы: ключ — адрес клиента, а их
    у сканера может быть сколько угодно.
    """
    for ip in [ip for ip, stamps in _login_fails.items()
               if not stamps or now - stamps[-1] > LOGIN_WINDOW]:
        _login_fails.pop(ip, None)


def login_allowed(ip: str) -> bool:
    now = time.time()
    with _login_lock:
        _prune_login_fails(now)
        stamps = [t for t in _login_fails.get(ip, []) if now - t <= LOGIN_WINDOW]
        _login_fails[ip] = stamps
        return len(stamps) < LOGIN_MAX_FAILURES


def login_failed(ip: str) -> None:
    now = time.time()
    with _login_lock:
        _login_fails.setdefault(ip, []).append(now)


def login_succeeded(ip: str) -> None:
    with _login_lock:
        _login_fails.pop(ip, None)


def _csrf_sig(ctx, issued: int) -> str:
    """Подпись токена формы.

    В расчёт входит сама cookie сессии, а не только uid: иначе токен
    оставался бы валидным вечно и для любой сессии этого пользователя —
    достаточно один раз утащить его из DOM. С привязкой к cookie новый вход
    (`set_session` выдаёт новую подпись) и выход обесценивают все ранее
    выданные токены.
    """
    session = ctx.cookies.get("session", "")
    uid = ctx.user["id"] if ctx.user else 0
    msg = f"csrf:{uid}:{session}:{issued}".encode()
    return hmac.new(SECRET_KEY, msg, hashlib.sha256).hexdigest()[:32]


def csrf_token(ctx) -> str:
    """Токен для формы: метка времени плюс подпись над ней."""
    issued = int(time.time())
    return f"{issued}.{_csrf_sig(ctx, issued)}"


def csrf_valid(ctx) -> bool:
    """Проверка токена из `ctx.form`: формат, срок и подпись."""
    issued_raw, _, sig = (ctx.form.get("csrf") or "").partition(".")
    if not sig:
        return False

    try:
        issued = int(issued_raw)
    except ValueError:
        return False

    # Слева небольшой допуск: страница могла быть отрисована на границе секунды.
    age = time.time() - issued
    if age < -60 or age > CSRF_TTL:
        return False

    return hmac.compare_digest(sig, _csrf_sig(ctx, issued))


# --------------------------------------------------------------------------- #
# HTML helpers
# --------------------------------------------------------------------------- #
def e(s) -> str:
    return html.escape("" if s is None else str(s))


PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
  :root {{ --bg:#f4f6fa; --card:#ffffff; --line:#dde2ea; --fg:#1b1f2a; --mut:#5c6779;
           --acc:#2f5fe0; --acc-hov:#1f47b8; --ok:#159a63; --off:#a6b0be; --danger:#d63a4e;
           --input:#ffffff; --soft:#eef1f6; --hover:#f2f5fb; --shadow:0 1px 2px rgba(20,30,60,.06); }}
  * {{ box-sizing:border-box; }}
  html {{ color-scheme:light; }}
  html,body {{ min-height:100%; }}
  body {{ margin:0; font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--fg); }}
  a {{ color:var(--acc); text-decoration:none; }}
  a:hover {{ color:var(--acc-hov); text-decoration:underline; }}
  header {{ display:flex; align-items:center; gap:16px; padding:14px 28px; border-bottom:1px solid var(--line); background:var(--card); box-shadow:var(--shadow); }}
  header .brand {{ font-weight:700; letter-spacing:.3px; color:var(--fg); }}
  header nav {{ margin-left:auto; display:flex; gap:18px; align-items:center; flex-wrap:wrap; }}
  main {{ width:100%; max-width:none; margin:0; padding:26px 28px 60px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:22px; margin:0 0 20px;
     box-shadow:var(--shadow); overflow-x:auto; }}
  h1 {{ font-size:22px; margin:0 0 18px; color:var(--fg); }}
  h2 {{ font-size:17px; margin:0 0 14px; color:var(--fg); }}
  label {{ display:block; font-size:13px; color:var(--mut); margin:12px 0 5px; }}
  input[type=text],input[type=email],input[type=password] {{
     width:100%; padding:10px 12px; background:var(--input); border:1px solid var(--line);
     border-radius:8px; color:var(--fg); font-size:14px; }}
  input[type=text]:focus,input[type=email]:focus,input[type=password]:focus {{
     outline:none; border-color:var(--acc); box-shadow:0 0 0 3px rgba(47,95,224,.15); }}
  input[type=file] {{ color:var(--mut); }}
  button,.btn {{ display:inline-block; cursor:pointer; border:0; border-radius:8px; padding:9px 15px;
     background:var(--acc); color:#fff; font-size:14px; font-weight:600; }}
  button:hover,.btn:hover {{ background:var(--acc-hov); color:#fff; text-decoration:none; }}
  .btn.sm {{ padding:5px 10px; font-size:12px; }}
  .btn.gray {{ background:var(--soft); color:var(--fg); border:1px solid var(--line); }}
  .btn.gray:hover {{ background:#e2e7f0; color:var(--fg); }}
  .btn.danger {{ background:var(--danger); color:#fff; }} .btn.danger:hover {{ background:#b32b3d; color:#fff; }}
  .btn.ok {{ background:var(--ok); color:#fff; }} .btn.ok:hover {{ background:#0f7d50; color:#fff; }}
  .btn:disabled,.btn.disabled {{ opacity:.45; cursor:not-allowed; }}
  table {{ width:100%; border-collapse:collapse; }}
  th,td {{ text-align:left; padding:9px 8px; border-bottom:1px solid var(--line); font-size:14px; vertical-align:middle; }}
  th {{ color:var(--mut); font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.4px; }}
  tbody tr:hover {{ background:var(--hover); }}
  .dot {{ display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; }}
  .dot.on {{ background:var(--ok); box-shadow:0 0 6px rgba(21,154,99,.55); }} .dot.off {{ background:var(--off); }}
  .pill {{ font-size:11px; padding:2px 8px; border-radius:20px; border:1px solid var(--line); color:var(--mut); background:var(--soft); }}
  .pill.admin {{ color:#8a5a00; border-color:#e7c67e; background:#fdf4e0; }}
  .pill.yes {{ color:#0f7d50; border-color:#a9dcc4; background:#e9f7f0; }}
  .pill.no {{ color:#b32b3d; border-color:#efb4bd; background:#fdecee; }}
  .flash {{ padding:11px 14px; border-radius:8px; margin:0 0 18px; font-size:14px; }}
  .flash.err {{ background:#fdecee; color:#8f2233; border:1px solid #efb4bd; }}
  .flash.ok {{ background:#e9f7f0; color:#14603f; border:1px solid #a9dcc4; }}
  .muted {{ color:var(--mut); font-size:13px; }}
  .row {{ display:flex; gap:10px; flex-wrap:wrap; align-items:center; }}
  .grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
  code {{ background:var(--soft); color:var(--fg); border:1px solid var(--line); padding:1px 6px; border-radius:5px; }}
  .crumb a::after {{ content:" / "; color:var(--mut); }}
  form.inline {{ display:inline; }}
  @media(max-width:640px){{ .grid2{{grid-template-columns:1fr;}} main{{padding:20px 14px 48px;}} header{{padding:12px 14px;}} }}
</style></head><body>
{header}
<main>{flash}{body}</main>
<script>
/* Текст подтверждения берётся из data-confirm, а НЕ из inline onsubmit.
   Значение приходит из данных пользователя (email, имя файла), а внутри
   onsubmit оно попадало бы сразу в два контекста — строку JS внутри
   HTML-атрибута, — где одного e() мало: браузер декодирует &#x27; обратно
   в апостроф до того, как строку увидит JS. В data-атрибуте контекст один,
   и e() его закрывает полностью. */
document.addEventListener('submit', function (ev) {{
  var form = ev.target;
  var msg = form && form.dataset ? form.dataset.confirm : null;
  if (msg && !confirm(msg)) {{ ev.preventDefault(); ev.stopPropagation(); }}
}}, true);
</script>
{script}
</body></html>"""


def render(title, body, user=None, flash=None, script=""):
    if user:
        header = (
            '<header><span class="brand">🖥️ Портал удалённого доступа</span><nav>'
            '<a href="/dashboard">Панель</a>'
            + ('<a href="/admin">Админ</a><a href="/files">Файлы</a>' if user["role"] == "admin" else "")
            + f'<span class="muted">{e(user["email"])}</span>'
            '<a href="/logout">Выход</a></nav></header>'
        )
    else:
        header = '<header><span class="brand">🖥️ Портал удалённого доступа</span></nav></header>'
    flash_html = ""
    if flash:
        kind, msg = flash
        flash_html = f'<div class="flash {kind}">{e(msg)}</div>'
    return PAGE.format(title=e(title), header=header, flash=flash_html, body=body, script=script)


# --------------------------------------------------------------------------- #
# Request context
# --------------------------------------------------------------------------- #
class Ctx:
    def __init__(self, handler: "Handler"):
        self.h = handler
        self.method = handler.command
        parsed = urlparse(handler.path)
        self.path = parsed.path
        self.query = parse_qs(parsed.query)
        self.cookies = self._parse_cookies(handler.headers.get("Cookie", ""))
        self.user = self._load_user()
        self.form = {}
        # Uploaded file parts: list of (field, filename, file-object).
        # A list (not a dict) so <input multiple> — every part carries the same
        # field name — keeps every file instead of only the last one.
        self.files = []
        self.raw_path = handler.path
        self._cookies = []

    @staticmethod
    def _parse_cookies(raw):
        out = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    def client_ip(self) -> str:
        """Адрес клиента с учётом того, что впереди стоит Caddy.

        Берём ПОСЛЕДНИЙ элемент X-Forwarded-For, а не первый: левые значения
        подставляет сам клиент, а правое дописывает наш прокси. И только если
        соединение пришло с петли — иначе заголовку верить нельзя вовсе.
        """
        peer = self.h.client_address[0] if self.h.client_address else ""

        if peer in ("127.0.0.1", "::1"):
            forwarded = self.h.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",")[-1].strip() or peer

        return peer

    def _load_user(self):
        tok = self.cookies.get("session", "")
        data = unsign(tok) if tok else None
        if not data:
            return None
        with db() as c:
            u = c.execute("SELECT * FROM users WHERE id=?", (data["uid"],)).fetchone()
        return dict(u) if u else None

    # ---- form / multipart parsing ----
    def read_body(self):
        length = int(self.h.headers.get("Content-Length", 0) or 0)
        ctype = self.h.headers.get("Content-Type", "")
        if ctype.startswith("multipart/form-data"):
            m = re.search(r"boundary=([^;]+)", ctype)
            if m:
                self._parse_multipart(length, m.group(1).strip().strip('"').encode())
            else:
                self._drain(length)
            return
        body = self.h.rfile.read(length) if length else b""
        for k, v in parse_qs(body.decode("utf-8", "replace")).items():
            self.form[k] = v[-1]

    def _drain(self, remaining):
        while remaining > 0:
            data = self.h.rfile.read(min(65536, remaining))
            if not data:
                break
            remaining -= len(data)

    def _parse_multipart(self, length, boundary):
        """Stream the body straight to disk.

        Reads in fixed-size chunks instead of slurping the whole request into
        memory, so a multi-hundred-megabyte upload does not blow up the process.
        """
        delim = b"--" + boundary
        term = b"\r\n" + delim
        rfile = self.h.rfile
        state = {"left": length, "buf": b""}

        def fill():
            if state["left"] <= 0:
                return False
            data = rfile.read(min(MULTIPART_CHUNK, state["left"]))
            if not data:
                state["left"] = 0
                return False
            state["left"] -= len(data)
            state["buf"] += data
            return True

        # Skip the preamble up to the first delimiter.
        while delim not in state["buf"]:
            if len(state["buf"]) > MULTIPART_SCAN_MAX:
                return  # boundary объявлен, но в теле его нет — не копим дальше
            if not fill():
                return
        state["buf"] = state["buf"].split(delim, 1)[1]

        while True:
            while len(state["buf"]) < 2 and fill():
                pass
            if state["buf"][:2] == b"--" or len(state["buf"]) < 2:
                return  # closing delimiter (or truncated body)
            if state["buf"][:2] == b"\r\n":
                state["buf"] = state["buf"][2:]
            while b"\r\n\r\n" not in state["buf"]:
                if len(state["buf"]) > MULTIPART_SCAN_MAX:
                    return  # заголовки части не заканчиваются — обрываем
                if not fill():
                    return
            head, state["buf"] = state["buf"].split(b"\r\n\r\n", 1)
            headers = head.decode("utf-8", "replace")
            name = re.search(r'name="([^"]*)"', headers)
            fname = re.search(r'filename="([^"]*)"', headers)
            sink = tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX) if fname else io.BytesIO()

            complete = False
            while True:
                idx = state["buf"].find(term)
                if idx != -1:
                    sink.write(state["buf"][:idx])
                    state["buf"] = state["buf"][idx + len(term):]
                    complete = True
                    break
                keep = len(term) - 1
                if len(state["buf"]) > keep:
                    sink.write(state["buf"][:-keep])
                    state["buf"] = state["buf"][-keep:]
                if not fill():
                    sink.write(state["buf"])
                    state["buf"] = b""
                    break

            if not name:
                sink.close()
            elif fname:
                if fname.group(1) and complete:
                    sink.seek(0)
                    self.files.append((name.group(1), fname.group(1), sink))
                else:
                    sink.close()  # empty file input, or a cut-off upload
            else:
                self.form[name.group(1)] = sink.getvalue().decode("utf-8", "replace")
                sink.close()
            if not complete:
                return

    def close_files(self):
        for _, _, fobj in self.files:
            try:
                fobj.close()
            except Exception:
                pass
        self.files = []

    # ---- responses ----
    def set_cookie(self, raw):
        self._cookies.append(raw)

    def set_session(self, uid):
        tok = sign({"uid": uid, "exp": int(time.time()) + SESSION_TTL})
        self.set_cookie(
            # Secure обязателен: портал опубликован только по HTTPS через Caddy,
            # а без флага один запрос по http:// до редиректа отдал бы сессию
            # тому, кто слушает сеть. http://localhost браузеры считают
            # доверенным контекстом, поэтому локальная отладка не ломается.
            f"session={tok}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age={SESSION_TTL}"
        )

    def clear_session(self):
        self.set_cookie("session=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0")

    def _emit_cookies(self):
        for c in self._cookies:
            self.h.send_header("Set-Cookie", c)

    #: Единственный внешний скрипт в PAGE — свой, инлайновый, поэтому
    #: 'unsafe-inline' для script-src обязателен; всё остальное запрещено.
    #: frame-ancestors 'none' закрывает кликджекинг админки: SameSite=Lax от
    #: него не спасает, форма во фрейме отправляется как same-site.
    SECURITY_HEADERS = (
        ("Content-Security-Policy",
         "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
         # connect-src обязателен: панель шлёт присутствие через
         # fetch('/api/heartbeat'), а без него оно молча перестанет работать.
         "script-src 'unsafe-inline'; connect-src 'self'; form-action 'self'; "
         "base-uri 'none'; frame-ancestors 'none'"),
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "same-origin"),
    )

    def _emit_disposition(self, filename):
        """Content-Disposition без инъекции в заголовок.

        В Linux имя файла может содержать кавычки и перевод строки. Оставляем
        безопасный ASCII-вариант, а полное имя отдаём отдельно по RFC 5987.
        """
        ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "download"
        self.h.send_header(
            "Content-Disposition",
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(filename, safe='')}",
        )

    def _emit_security_headers(self):
        for name, value in self.SECURITY_HEADERS:
            self.h.send_header(name, value)

    def send_html(self, html_str, status=200):
        data = html_str.encode()
        self.h.send_response(status)
        self.h.send_header("Content-Type", "text/html; charset=utf-8")
        self.h.send_header("Content-Length", str(len(data)))
        self._emit_security_headers()
        self._emit_cookies()
        self.h.end_headers()
        self.h.wfile.write(data)

    def redirect(self, location):
        self.h.send_response(303)
        self.h.send_header("Location", location)
        self._emit_cookies()
        self.h.end_headers()

    def send_bytes(self, data, ctype, filename=None):
        self.h.send_response(200)
        self.h.send_header("Content-Type", ctype)
        self.h.send_header("Content-Length", str(len(data)))
        if filename:
            self._emit_disposition(filename)
        self.h.end_headers()
        self.h.wfile.write(data)

    def send_stream(self, fileobj, size, ctype, filename=None):
        """Отдать открытый файл потоком.

        Раньше и скачивание, и ZIP собирались в память целиком: файл на
        несколько гигабайт клал процесс. Здесь наружу уходит фиксированными
        кусками, в памяти лежит только один кусок.
        """
        self.h.send_response(200)
        self.h.send_header("Content-Type", ctype)
        self.h.send_header("Content-Length", str(size))
        self._emit_security_headers()
        if filename:
            self._emit_disposition(filename)
        self.h.end_headers()
        shutil.copyfileobj(fileobj, self.h.wfile, 64 * 1024)

    def send_json(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.h.send_response(status)
        self.h.send_header("Content-Type", "application/json")
        self.h.send_header("Content-Length", str(len(data)))
        self.h.end_headers()
        self.h.wfile.write(data)


# --------------------------------------------------------------------------- #
# Flash messages via one-shot query param
# --------------------------------------------------------------------------- #
def flash_from_query(ctx):
    if "ok" in ctx.query:
        return ("ok", ctx.query["ok"][0])
    if "err" in ctx.query:
        return ("err", ctx.query["err"][0])
    return None


def redirect_flash(ctx, path, kind, msg):
    ctx.redirect(f"{path}{'&' if '?' in path else '?'}{kind}={quote(msg)}")


# --------------------------------------------------------------------------- #
# File-manager helpers (sandboxed to FILES_ROOT)
# --------------------------------------------------------------------------- #
def safe_path(rel):
    rel = (rel or "").lstrip("/")
    full = os.path.realpath(os.path.join(FILES_ROOT, rel))
    if full != FILES_ROOT and not full.startswith(FILES_ROOT + os.sep):
        return None
    return full


def rel_of(full):
    return os.path.relpath(full, FILES_ROOT).replace(os.sep, "/")


# =========================================================================== #
# Views
# =========================================================================== #
def registration_open() -> bool:
    """Открыта ли самостоятельная регистрация.

    По умолчанию закрыта: портал смотрит в интернет, а свободная регистрация —
    готовый канал доставки для любой находки в админ-панели и заодно оракул на
    занятость адреса. Пустая база — исключение: иначе первый администратор не
    появится вовсе.

    Открыть обратно: ALLOW_REGISTRATION=true в юните systemd.
    """
    if ALLOW_REGISTRATION:
        return True

    with db() as c:
        return c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"] == 0


def view_index(ctx):
    if ctx.user:
        return ctx.redirect("/dashboard")
    can_register = registration_open()
    # ?tab=register при закрытой регистрации не должен показывать пустую вкладку.
    tab = ctx.query.get("tab", ["login"])[0] if can_register else "login"

    tabs = ""
    if can_register:
        login_cls = "gray" if tab == "register" else ""
        reg_cls = "gray" if tab != "register" else ""
        tabs = (
            '<div class="row" style="margin:16px 0;">'
            f'<a class="btn {login_cls}" href="/?tab=login">Войти</a>'
            f'<a class="btn {reg_cls}" href="/?tab=register">Регистрация</a>'
            "</div>"
        )

    lead = ("Войдите или создайте аккаунт для доступа к удалённым рабочим столам."
            if can_register else "Войдите для доступа к удалённым рабочим столам.")

    closed_note = "" if can_register else (
        '<p class="muted" style="margin-top:14px;">'
        "Регистрация закрыта. Учётную запись создаёт администратор.</p>"
    )

    body = f"""
    <div class="card" style="max-width:440px;margin:40px auto;">
      <h1>Портал удалённого доступа</h1>
      <p class="muted">{lead}</p>
      {tabs}
      {'' if tab=='register' else login_form()}
      {register_form() if tab=='register' else ''}
      {closed_note}
    </div>"""
    return ctx.send_html(render("Вход", body, flash=flash_from_query(ctx)))


def login_form():
    return """
    <form method="post" action="/login">
      <label>Эл. почта</label><input type="email" name="email" required autofocus>
      <label>Пароль</label><input type="password" name="password" required>
      <div style="margin-top:18px;"><button type="submit">Войти</button></div>
    </form>"""


def register_form():
    return """
    <form method="post" action="/register">
      <label>Эл. почта</label><input type="email" name="email" required autofocus>
      <label>Пароль (минимум 8 символов)</label><input type="password" name="password" minlength="8" required>
      <div style="margin-top:18px;"><button type="submit">Создать аккаунт</button></div>
      <p class="muted" style="margin-top:12px;">Первый созданный аккаунт становится администратором.</p>
    </form>"""


def action_register(ctx):
    # Проверка именно здесь, а не только в разметке: форму можно отправить и
    # мимо страницы, напрямую в POST /register.
    if not registration_open():
        return redirect_flash(ctx, "/", "err",
                              "Регистрация закрыта. Обратитесь к администратору.")

    email = (ctx.form.get("email") or "").strip().lower()
    pw = ctx.form.get("password") or ""
    if not re.match(EMAIL_RE, email):
        return redirect_flash(ctx, "/?tab=register", "err", "Введите корректный адрес эл. почты.")
    if len(pw) < MIN_PASSWORD_LEN:
        return redirect_flash(ctx, "/?tab=register", "err",
                              f"Пароль должен содержать не менее {MIN_PASSWORD_LEN} символов.")
    pw_hash, pw_salt = hash_password(pw)
    with db() as c:
        first = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"] == 0
        role = "admin" if first else "user"
        remote = 1 if first else 0
        try:
            cur = c.execute(
                "INSERT INTO users(email,pw_hash,pw_salt,role,remote_allowed,created_at) VALUES(?,?,?,?,?,?)",
                (email, pw_hash, pw_salt, role, remote, int(time.time())),
            )
        except sqlite3.IntegrityError:
            return redirect_flash(ctx, "/?tab=register", "err", "Этот email уже зарегистрирован.")
        uid = cur.lastrowid
    ctx.set_session(uid)
    post_login_redirect(ctx)


def action_login(ctx):
    email = (ctx.form.get("email") or "").strip().lower()
    pw = ctx.form.get("password") or ""

    # Считаем по адресу клиента, а не по email: перебор идёт по обоим полям,
    # а блокировать чужую учётку чужими попытками нельзя.
    client = ctx.client_ip()
    if not login_allowed(client):
        return redirect_flash(ctx, "/?tab=login", "err",
                              "Слишком много попыток входа. Попробуйте через несколько минут.")

    with db() as c:
        u = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not u or not verify_password(pw, u["pw_hash"], u["pw_salt"]):
        login_failed(client)
        return redirect_flash(ctx, "/?tab=login", "err", "Неверный email или пароль.")

    login_succeeded(client)
    ctx.set_session(u["id"])
    post_login_redirect(ctx)


def action_logout(ctx):
    ctx.clear_session()
    ctx.redirect("/")


# --------------------------------------------------------------------------- #
# Развёртывание агента RustDesk на управляемой машине
# --------------------------------------------------------------------------- #
def rustdesk_key():
    """Публичный ключ сервера: переменная окружения, иначе файл рядом с кодом."""
    key = os.environ.get("RUSTDESK_KEY", "").strip()
    if key:
        return key
    try:
        with open(RUSTDESK_KEY_FILE, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def rustdesk_config_string(key):
    """Строка для `rustdesk.exe --config`.

    Формат разобран по исходнику клиента (`src/custom_server.rs`,
    `get_custom_server_from_config_string`): строку переворачивают, декодируют как
    base64url без выравнивания и разбирают как JSON. Подпись там проверяется
    только если JSON не разобрался, поэтому неподписанной строки достаточно —
    Pro-сервер и его генератор конфигов для этого не нужны.
    """
    payload = json.dumps(
        {"host": RUSTDESK_HOST, "key": key, "api": CORTENDESK_URL, "relay": ""},
        separators=(",", ":"),
    )
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return encoded[::-1]


def ps_quote(s):
    """Значение внутри одинарных кавычек PowerShell."""
    return str(s).replace("'", "''")


def portal_base(ctx):
    """Публичный адрес самого портала — не CortenDesk.

    Берётся из заголовков запроса, потому что за Caddy своего адреса процесс не
    знает: он слушает 127.0.0.1:8000. OIDC_ISSUER — запасной вариант, это тот же
    портал с другой стороны.
    """
    host = ctx.h.headers.get("X-Forwarded-Host") or ctx.h.headers.get("Host", "")
    if not host:
        return OIDC_ISSUER
    proto = ctx.h.headers.get("X-Forwarded-Proto", "http")
    return f"{proto}://{host}".rstrip("/")


#: Скрипт развёртывания для Windows. Порядок шагов и имена ключей взяты из
#: официальной инструкции RustDesk (doc/self-host/client-deployment): тихая
#: установка, служба `Rustdesk`, затем --config и --password. Подстановки идут
#: через @@…@@, а не через format(): в PowerShell фигурных скобок слишком много.
WINDOWS_SETUP_PS1 = r"""#Requires -Version 5.1
<#
    CortenDesk — постоянный доступ к этой машине.

    Ставит RustDesk @@VER@@ службой, прописывает сервер @@HOST@@ и постоянный
    пароль. После этого к машине можно подключаться с загрузки: запускать
    RustDesk вручную и держать его окно открытым больше не нужно.

    Запускать на ТОЙ машине, к которой подключаются. Права администратора
    скрипт запросит сам.
#>

$ErrorActionPreference = 'SilentlyContinue'

$rustdesk_cfg = '@@CFG@@'
$rustdesk_pw  = '@@PW@@'
$rustdesk_url = 'https://github.com/rustdesk/rustdesk/releases/download/@@VER@@/rustdesk-@@VER@@-x86_64.exe'

# --- права администратора ---------------------------------------------------
$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'Перезапускаю с правами администратора...'
    Start-Process PowerShell -Verb RunAs -ArgumentList `
        "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}

if ($rustdesk_pw -eq '') {
    $rustdesk_pw = -join ((65..90) + (97..122) + (48..57) | Get-Random -Count 14 | % {[char]$_})
    Write-Host 'Постоянный пароль не задан в портале — сгенерирован случайный.'
}

# --- установка --------------------------------------------------------------
$exe = "$env:ProgramFiles\RustDesk\rustdesk.exe"
if (-not (Test-Path $exe)) {
    $tmp = Join-Path $env:TEMP 'rustdesk-setup.exe'
    Write-Host "Скачиваю RustDesk @@VER@@..."
    try {
        Invoke-WebRequest -Uri $rustdesk_url -OutFile $tmp -UseBasicParsing -ErrorAction Stop
    } catch {
        Write-Host "ОШИБКА: не удалось скачать $rustdesk_url"
        Write-Host $_.Exception.Message
        Read-Host 'Enter для выхода'
        exit 1
    }
    Write-Host 'Устанавливаю (тихий режим)...'
    Start-Process $tmp -ArgumentList '--silent-install' -Wait
    Start-Sleep -Seconds 20
} else {
    Write-Host 'RustDesk уже установлен.'
}

if (-not (Test-Path $exe)) {
    Write-Host "ОШИБКА: после установки нет $exe"
    Read-Host 'Enter для выхода'
    exit 1
}

# --- служба: она и есть «без запущенного RustDesk» --------------------------
if (-not (Get-Service -Name 'Rustdesk' -ErrorAction SilentlyContinue)) {
    Write-Host 'Регистрирую службу...'
    Start-Process $exe -ArgumentList '--install-service' -Wait
    Start-Sleep -Seconds 20
}
Set-Service -Name 'Rustdesk' -StartupType Automatic
Start-Service -Name 'Rustdesk'
Start-Sleep -Seconds 5

# --- настройки сервера и пароль ---------------------------------------------
Write-Host 'Прописываю сервер CortenDesk...'
& $exe --config $rustdesk_cfg
Start-Sleep -Seconds 2
& $exe --password $rustdesk_pw
Start-Sleep -Seconds 2
Restart-Service -Name 'Rustdesk'
Start-Sleep -Seconds 5

$rustdesk_id = (& $exe --get-id | Out-String).Trim()

Write-Host ''
Write-Host '======================================================'
Write-Host "  ID этой машины : $rustdesk_id"
Write-Host "  Пароль         : $rustdesk_pw"
Write-Host '======================================================'
Write-Host ''
Write-Host 'Впишите этот ID в портале, в поле «Мой RustDesk ID»:'
Write-Host '  @@PORTAL@@'
Write-Host ''
Read-Host 'Enter для выхода'
"""


def action_windows_setup(ctx):
    """Отдать готовый скрипт развёртывания агента под Windows."""
    u = require_login(ctx)
    if not u:
        return
    key = rustdesk_key()
    if not key or not RUSTDESK_HOST:
        return redirect_flash(
            ctx, "/dashboard", "err",
            "Не задан адрес или ключ сервера RustDesk — скрипт собрать нечем.",
        )
    script = (
        WINDOWS_SETUP_PS1
        .replace("@@CFG@@", ps_quote(rustdesk_config_string(key)))
        .replace("@@PW@@", ps_quote(u["rustdesk_pw"] or ""))
        .replace("@@VER@@", ps_quote(RUSTDESK_VERSION))
        .replace("@@HOST@@", ps_quote(RUSTDESK_HOST))
        .replace("@@PORTAL@@", ps_quote(portal_base(ctx) + "/dashboard"))
    )
    # BOM: Windows PowerShell 5.1 читает файл без него в ANSI и ломает кириллицу.
    ctx.send_bytes(
        script.encode("utf-8-sig"),
        "application/octet-stream",
        "cortendesk-windows-setup.ps1",
    )


def view_dashboard(ctx):
    u = require_login(ctx)
    if not u:
        return
    now = int(time.time())
    with db() as c:
        peers = c.execute(
            "SELECT * FROM users WHERE id!=? AND remote_allowed=1 ORDER BY last_seen DESC, email",
            (u["id"],),
        ).fetchall()
    rows = ""
    for p in peers:
        online = (now - p["last_seen"]) <= ONLINE_WINDOW
        has_id = bool(p["rustdesk_id"])
        connect_url = CONNECT_URL_TEMPLATE.format(base=CORTENDESK_URL, id=quote(p["rustdesk_id"]))
        if online and has_id:
            btn = f'<a class="btn sm ok" href="{e(connect_url)}" target="_blank" rel="noopener">Подключиться ▸</a>'
        elif not has_id:
            btn = '<span class="btn sm disabled">Нет ID устройства</span>'
        else:
            btn = '<span class="btn sm disabled">Офлайн</span>'
        rows += (
            f'<tr><td><span class="dot {"on" if online else "off"}"></span>{e(p["email"])}</td>'
            f'<td><code>{e(p["rustdesk_id"] or "—")}</code></td>'
            f'<td>{"онлайн" if online else "офлайн"}</td><td>{btn}</td></tr>'
        )
    if not rows:
        rows = '<tr><td colspan="4" class="muted">Пока ни у кого не включён удалённый доступ.</td></tr>'

    my_remote = (
        '<span class="pill yes">включён</span>'
        if u["remote_allowed"]
        else '<span class="pill no">выключен — обратитесь к админу</span>'
    )
    token = csrf_token(ctx)
    # Кнопка гаснет, а не исчезает: молча пропавшая кнопка выглядит как поломка,
    # а причина («нет ключа сервера») чинится одной настройкой.
    if rustdesk_key() and RUSTDESK_HOST:
        win_btn = (
            '<a class="btn" href="/device/windows" download>Скачать скрипт для Windows</a>'
            '<p class="muted" style="margin-top:8px;">'
            'Правой кнопкой по файлу → «Выполнить с помощью PowerShell». '
            'Права администратора скрипт запросит сам.</p>'
        )
    else:
        win_btn = (
            '<span class="btn disabled">Скачать скрипт для Windows</span>'
            '<p class="muted" style="margin-top:8px;">'
            'Недоступно: не задан ключ сервера RustDesk '
            f'(<code>RUSTDESK_KEY</code> или файл <code>{e(os.path.basename(RUSTDESK_KEY_FILE))}</code>).</p>'
        )
    body = f"""
    <h1>Панель управления</h1>
    <div class="grid2">
      <div class="card">
        <h2>Моё устройство</h2>
        <p class="muted">Удалённый доступ к <b>вашему</b> рабочему столу: {my_remote}</p>
        <form method="post" action="/device">
          <input type="hidden" name="csrf" value="{token}">
          <label>Мой RustDesk ID</label>
          <input type="text" name="rustdesk_id" value="{e(u['rustdesk_id'])}" placeholder="например, 123456789">
          <label>Мой пароль RustDesk (постоянный)</label>
          <input type="password" name="rustdesk_pw" autocomplete="new-password"
                 placeholder="{'сохранён — оставьте пустым, чтобы не менять' if u['rustdesk_pw'] else 'задайте в клиенте RustDesk'}">
          <div style="margin-top:16px;"><button type="submit">Сохранить</button></div>
        </form>
        <p class="muted" style="margin-top:12px;">
          Установите клиент RustDesk, укажите в нём ваш сервер CortenDesk, затем
          вставьте его ID сюда, чтобы к вам могли подключаться.</p>
        <hr style="border:0;border-top:1px solid #e5e7eb;margin:16px 0;">
        <h3 style="margin:0 0 6px;font-size:15px;">Доступ без запущенного RustDesk</h3>
        <p class="muted" style="margin:0 0 10px;">
          Скрипт ставит RustDesk {e(RUSTDESK_VERSION)} <b>службой</b>, прописывает сервер
          и постоянный пароль. После него машина доступна с загрузки — держать
          окно RustDesk открытым не нужно. Запускать на той машине, к которой
          подключаются.</p>
        {win_btn}
      </div>
      <div class="card">
        <h2>Как подключиться</h2>
        <ol class="muted" style="padding-left:18px;">
          <li>Админ должен разрешить удалённый доступ нужному пользователю.</li>
          <li>Пользователь должен быть <b>онлайн</b> (эта страница шлёт сигнал присутствия).</li>
          <li>Нажмите <b>Подключиться</b> — откроется веб-клиент CortenDesk для этого ID.</li>
        </ol>
        <p class="muted">Сервер CortenDesk: <code>{e(CORTENDESK_URL)}</code></p>
      </div>
    </div>
    <div class="card">
      <h2>Доступные рабочие столы</h2>
      <table><thead><tr><th>Пользователь</th><th>ID устройства</th><th>Статус</th><th></th></tr></thead>
      <tbody>{rows}</tbody></table>
    </div>"""
    script = """<script>
      function beat(){ fetch('/api/heartbeat',{method:'POST'}).catch(()=>{}); }
      beat(); setInterval(()=>{ beat(); }, 20000);
      setInterval(()=>{ location.reload(); }, 30000);
    </script>"""
    return ctx.send_html(render("Панель", body, user=u, flash=flash_from_query(ctx), script=script))


def action_device(ctx):
    u = require_login(ctx)
    if not u:
        return
    if not csrf_valid(ctx):
        return redirect_flash(ctx, "/dashboard", "err", "Неверный токен формы.")
    rid = (ctx.form.get("rustdesk_id") or "").strip()
    # Значение в разметку больше не возвращается, поэтому пустое поле означает
    # «не менять», а не «стереть»: иначе сохранение ID стирало бы пароль.
    rpw = (ctx.form.get("rustdesk_pw") or "").strip()
    with db() as c:
        if rpw:
            c.execute("UPDATE users SET rustdesk_id=?, rustdesk_pw=? WHERE id=?", (rid, rpw, u["id"]))
        else:
            c.execute("UPDATE users SET rustdesk_id=? WHERE id=?", (rid, u["id"]))
    redirect_flash(ctx, "/dashboard", "ok", "Настройки устройства сохранены.")


def api_heartbeat(ctx):
    if not ctx.user:
        return ctx.send_json({"ok": False}, 401)
    with db() as c:
        c.execute("UPDATE users SET last_seen=? WHERE id=?", (int(time.time()), ctx.user["id"]))
    ctx.send_json({"ok": True})


# ---------------------------- Admin -------------------------------------- #
def view_admin(ctx):
    u = require_admin(ctx)
    if not u:
        return
    now = int(time.time())
    with db() as c:
        users = c.execute("SELECT * FROM users ORDER BY id").fetchall()
    token = csrf_token(ctx)
    rows = ""
    for x in users:
        online = (now - x["last_seen"]) <= ONLINE_WINDOW
        is_self = x["id"] == u["id"]
        role_pill = (
            '<span class="pill admin">админ</span>' if x["role"] == "admin" else '<span class="pill">пользователь</span>'
        )
        remote_pill = (
            '<span class="pill yes">разрешён</span>' if x["remote_allowed"] else '<span class="pill no">запрещён</span>'
        )
        last = "онлайн" if online else (time.strftime("%Y-%m-%d %H:%M", time.localtime(x["last_seen"])) if x["last_seen"] else "никогда")

        def act(action, label, cls="gray", confirm=None):
            oc = f' data-confirm="{e(confirm)}"' if confirm else ""
            return (
                f'<form class="inline" method="post" action="/admin/action"{oc}>'
                f'<input type="hidden" name="csrf" value="{token}">'
                f'<input type="hidden" name="uid" value="{x["id"]}">'
                f'<input type="hidden" name="action" value="{action}">'
                f'<button class="btn sm {cls}" type="submit">{label}</button></form> '
            )

        client_btn = ""
        if x["rustdesk_id"]:
            client_btn = (
                f'<a class="btn sm" href="rustdesk://{e(x["rustdesk_id"])}" '
                f'title="Открыть в десктоп-клиенте RustDesk (нужен установленный клиент)">🖥️ Клиент ▸</a> '
            )
        actions = client_btn + act("toggle_remote", "Запретить доступ" if x["remote_allowed"] else "Разрешить доступ",
                      "danger" if x["remote_allowed"] else "ok")
        if not is_self:
            actions += act("toggle_role", "Сделать пользователем" if x["role"] == "admin" else "Сделать админом")
            actions += act("delete", "Удалить", "danger", confirm=f"Удалить {x['email']}?")
        else:
            actions += '<span class="muted" style="font-size:12px;">(вы)</span>'

        rows += (
            f'<tr><td>{x["id"]}</td>'
            f'<td><span class="dot {"on" if online else "off"}"></span>{e(x["email"])}</td>'
            f'<td>{role_pill}</td><td>{remote_pill}</td>'
            f'<td><code>{e(x["rustdesk_id"] or "—")}</code></td>'
            f'<td class="muted">{e(last)}</td><td class="row">{actions}</td></tr>'
        )
    # Пароль предлагаем сами: иначе админ впишет «12345678», а сменить его
    # пользователь потом не сможет — механизма смены в портале нет. Страница
    # перезагружается раз в 15 с, так что заготовка обновляется — это нормально.
    suggested_pw = secrets.token_urlsafe(9)

    body = f"""
    <h1>Админ — Панель управления</h1>
    <div class="card">
      <h2>Пользователи и статус</h2>
      <table><thead><tr><th>#</th><th>Пользователь</th><th>Роль</th><th>Доступ</th>
      <th>ID устройства</th><th>Был(а)</th><th>Действия</th></tr></thead>
      <tbody>{rows}</tbody></table>
      <p class="muted" style="margin-top:14px;">
        Пользователь отмечается <span class="dot on"></span>онлайн, если его браузер прислал сигнал
        присутствия за последние {ONLINE_WINDOW} с. «Разрешить доступ» — другие смогут подключаться к его рабочему столу.</p>
    </div>
    <div class="card">
      <h2>Создать пользователя</h2>
      <p class="muted">Самостоятельная регистрация закрыта, учётные записи заводит администратор.
        Пароль сообщите пользователю — сменить его он сможет только через вас.</p>
      <form method="post" action="/admin/create">
        <input type="hidden" name="csrf" value="{token}">
        <div class="grid2">
          <div>
            <label>Эл. почта</label>
            <input type="email" name="email" required placeholder="user@example.org">
          </div>
          <div>
            <label>Пароль (минимум {MIN_PASSWORD_LEN} символов)</label>
            <input type="text" name="password" minlength="{MIN_PASSWORD_LEN}" required value="{suggested_pw}">
          </div>
        </div>
        <div class="row" style="margin-top:14px;align-items:center;gap:18px;">
          <label style="margin:0;"><input type="checkbox" name="is_admin" value="1"> Сделать администратором</label>
          <label style="margin:0;"><input type="checkbox" name="remote_allowed" value="1"> Разрешить удалённый доступ</label>
        </div>
        <div style="margin-top:16px;"><button type="submit">Создать</button></div>
      </form>
    </div>"""
    script = "<script>setInterval(()=>location.reload(),15000);</script>"
    return ctx.send_html(render("Админ", body, user=u, flash=flash_from_query(ctx), script=script))


def action_admin(ctx):
    u = require_admin(ctx)
    if not u:
        return
    if not csrf_valid(ctx):
        return redirect_flash(ctx, "/admin", "err", "Неверный токен формы.")
    try:
        target = int(ctx.form.get("uid", ""))
    except ValueError:
        return redirect_flash(ctx, "/admin", "err", "Некорректный ID пользователя.")
    action = ctx.form.get("action")
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (target,)).fetchone()
        if not row:
            return redirect_flash(ctx, "/admin", "err", "Пользователь не найден.")
        if action == "toggle_remote":
            c.execute("UPDATE users SET remote_allowed=? WHERE id=?", (0 if row["remote_allowed"] else 1, target))
            msg = f"Удалённый доступ {'запрещён' if row['remote_allowed'] else 'разрешён'} для {row['email']}."
        elif action == "toggle_role" and target != u["id"]:
            c.execute("UPDATE users SET role=? WHERE id=?", ("user" if row["role"] == "admin" else "admin", target))
            msg = f"Роль обновлена для {row['email']}."
        elif action == "delete" and target != u["id"]:
            c.execute("DELETE FROM users WHERE id=?", (target,))
            msg = f"Удалён {row['email']}."
        else:
            return redirect_flash(ctx, "/admin", "err", "Действие запрещено.")
    redirect_flash(ctx, "/admin", "ok", msg)


def action_admin_create(ctx):
    """Создание учётной записи админом.

    Появилось вместе с закрытием самостоятельной регистрации: без этой формы
    добавить пользователя можно было бы только через ALLOW_REGISTRATION=true
    и перезапуск сервиса.

    Проверки email и пароля — те же, что в `action_register`, намеренно: два
    разных набора правил для одного поля разъезжаются при первой же правке.
    """
    u = require_admin(ctx)
    if not u:
        return
    if not csrf_valid(ctx):
        return redirect_flash(ctx, "/admin", "err", "Неверный токен формы.")

    email = (ctx.form.get("email") or "").strip().lower()
    pw = ctx.form.get("password") or ""
    if not re.match(EMAIL_RE, email):
        return redirect_flash(ctx, "/admin", "err", "Введите корректный адрес эл. почты.")
    if len(pw) < MIN_PASSWORD_LEN:
        return redirect_flash(ctx, "/admin", "err",
                              f"Пароль должен содержать не менее {MIN_PASSWORD_LEN} символов.")

    role = "admin" if ctx.form.get("is_admin") else "user"
    remote = 1 if ctx.form.get("remote_allowed") else 0
    pw_hash, pw_salt = hash_password(pw)

    with db() as c:
        try:
            c.execute(
                "INSERT INTO users(email,pw_hash,pw_salt,role,remote_allowed,created_at) VALUES(?,?,?,?,?,?)",
                (email, pw_hash, pw_salt, role, remote, int(time.time())),
            )
        except sqlite3.IntegrityError:
            return redirect_flash(ctx, "/admin", "err", "Этот email уже зарегистрирован.")

    redirect_flash(ctx, "/admin", "ok",
                   f"Создан {email}" + (" (администратор)" if role == "admin" else "") + ".")

# ---------------------------- Files -------------------------------------- #
def view_files(ctx):
    u = require_admin(ctx)
    if not u:
        return
    rel = ctx.query.get("path", [""])[0]
    full = safe_path(rel)
    if not full or not os.path.isdir(full):
        return redirect_flash(ctx, "/files", "err", "Каталог не найден.")
    cur_rel = rel_of(full)
    cur_rel = "" if cur_rel == "." else cur_rel
    token = csrf_token(ctx)

    # Крошки: корень — имя каталога FILES_ROOT, дальше каждый сегмент пути.
    crumbs = f'<a href="/files">🏠 {e(ROOT_LABEL)}</a>'
    accum = ""
    for part in [p for p in cur_rel.split("/") if p]:
        accum = f"{accum}/{part}".lstrip("/")
        crumbs += f'<a href="/files?path={quote(accum)}">{e(part)}</a>'

    # Корнем может быть любой каталог хоста, поэтому чужие права — норма,
    # а не авария: показываем пустую папку с пояснением вместо 500.
    denied = False
    try:
        names = os.listdir(full)
    except OSError:
        names, denied = [], True
    entries = sorted(names, key=lambda n: (not os.path.isdir(os.path.join(full, n)), n.lower()))

    parent = os.path.dirname(cur_rel)
    up_btn = (
        f'<a class="btn sm gray" href="/files?path={quote(parent)}">⬆ Наверх</a>'
        if cur_rel else '<span class="btn sm gray disabled">⬆ Наверх</span>'
    )

    rows = ""
    if cur_rel:
        rows += f'<tr><td colspan="4"><a href="/files?path={quote(parent)}">⬅ ..</a></td></tr>'
    for name in entries:
        p = os.path.join(full, name)
        child_rel = rel_of(p)
        if os.path.isdir(p):
            rows += (
                f'<tr><td>📁 <a href="/files?path={quote(child_rel)}">{e(name)}</a></td>'
                f'<td class="muted">папка</td><td></td>'
                f'<td><a class="btn sm gray" href="/files/zip?path={quote(child_rel)}">ZIP ▾</a></td></tr>'
            )
        else:
            try:
                size = os.path.getsize(p)
            except OSError:      # битая ссылка или исчезающий файл
                size = 0
            rows += (
                f'<tr><td>📄 {e(name)}</td><td class="muted">{fmt_size(size)}</td>'
                f'<td><a class="btn sm gray" href="/files/download?path={quote(child_rel)}">Скачать</a></td>'
                f'<td><form class="inline" method="post" action="/files/delete" '
                f'data-confirm="Удалить {e(name)}?">'
                f'<input type="hidden" name="csrf" value="{token}">'
                f'<input type="hidden" name="path" value="{e(child_rel)}">'
                f'<button class="btn sm danger" type="submit">Удалить</button></form></td></tr>'
            )
    if not entries:
        # Строка «..» уже занимает rows, поэтому пустоту определяем по entries.
        msg = "Нет доступа к этой папке." if denied else "Пустая папка."
        rows += f'<tr><td colspan="4" class="muted">{msg}</td></tr>'

    body = f"""
    <h1>Файлы</h1>
    <div class="card">
      <div class="row" style="justify-content:space-between;">
        <div class="crumb">{crumbs}</div>
        <div class="row">
          {up_btn}
          <a class="btn ok" href="/files/zip?path={quote(cur_rel)}">Скачать папку как ZIP</a>
        </div>
      </div>
      <form method="get" action="/files" class="row" style="margin:12px 0 4px;">
        <input type="text" name="path" value="{e(cur_rel)}"
               placeholder="путь от корня, например webportal/files" style="flex:1;min-width:220px;">
        <button class="btn gray" type="submit">Перейти</button>
      </form>
      <p class="muted">Корень: <code>{e(FILES_ROOT)}</code> — текущая папка: <code>{e("/" + cur_rel if cur_rel else "/")}</code></p>
      <table><thead><tr><th>Имя</th><th>Размер</th><th>Скачать</th><th></th></tr></thead>
      <tbody>{rows}</tbody></table>
    </div>
    <div class="card">
      <h2>Загрузить в эту папку</h2>
      <form method="post" action="/files/upload?path={quote(cur_rel)}" enctype="multipart/form-data">
        <input type="hidden" name="csrf" value="{token}">
        <input type="file" name="file" multiple required style="margin-bottom:14px;color:var(--mut)">
        <div><button type="submit">Загрузить</button></div>
      </form>
      <h2 style="margin-top:22px;">Новая папка</h2>
      <form method="post" action="/files/mkdir?path={quote(cur_rel)}" class="row">
        <input type="hidden" name="csrf" value="{token}">
        <input type="text" name="name" placeholder="имя папки" required style="max-width:260px;">
        <button type="submit">Создать</button>
      </form>
    </div>"""
    return ctx.send_html(render("Файлы", body, user=u, flash=flash_from_query(ctx)))


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def action_upload(ctx):
    u = require_admin(ctx)
    if not u:
        return
    if not csrf_valid(ctx):
        return redirect_flash(ctx, "/files", "err", "Неверный токен формы.")
    rel = ctx.query.get("path", [""])[0]
    dest = safe_path(rel)
    if not dest or not os.path.isdir(dest):
        return redirect_flash(ctx, "/files", "err", "Некорректная папка назначения.")
    count = 0
    for _, fname, fobj in ctx.files:
        safe_name = os.path.basename(fname.replace("\\", "/")).strip()
        if not safe_name or safe_name in (".", ".."):
            continue
        target = safe_path(os.path.join(rel, safe_name))
        if not target or os.path.isdir(target):
            continue
        with open(target, "wb") as f:
            shutil.copyfileobj(fobj, f, MULTIPART_CHUNK)
        count += 1
    if not count:
        return redirect_flash(ctx, f"/files?path={quote(rel)}", "err", "Не выбрано ни одного файла.")
    redirect_flash(ctx, f"/files?path={quote(rel)}", "ok", f"Загружено файлов: {count}.")


def action_mkdir(ctx):
    u = require_admin(ctx)
    if not u:
        return
    if not csrf_valid(ctx):
        return redirect_flash(ctx, "/files", "err", "Неверный токен формы.")
    rel = ctx.query.get("path", [""])[0]
    name = os.path.basename((ctx.form.get("name") or "").strip())
    if not name:
        return redirect_flash(ctx, f"/files?path={quote(rel)}", "err", "Некорректное имя папки.")
    target = safe_path(os.path.join(rel, name))
    if not target:
        return redirect_flash(ctx, "/files", "err", "Некорректный путь.")
    os.makedirs(target, exist_ok=True)
    redirect_flash(ctx, f"/files?path={quote(rel)}", "ok", f"Создана папка «{name}».")


def action_delete(ctx):
    u = require_admin(ctx)
    if not u:
        return
    if not csrf_valid(ctx):
        return redirect_flash(ctx, "/files", "err", "Неверный токен формы.")
    rel = ctx.form.get("path", "")
    target = safe_path(rel)
    parent = quote(os.path.dirname(rel))
    if not target or not os.path.isfile(target):
        return redirect_flash(ctx, "/files", "err", "Файл не найден.")
    os.remove(target)
    redirect_flash(ctx, f"/files?path={parent}", "ok", "Файл удалён.")


def action_download(ctx):
    if not require_admin(ctx):
        return
    rel = ctx.query.get("path", [""])[0]
    target = safe_path(rel)
    if not target or not os.path.isfile(target):
        return ctx.send_html(render("Не найдено", "<div class='card'>Файл не найден.</div>"), 404)
    with open(target, "rb") as f:
        ctx.send_stream(f, os.path.getsize(target), "application/octet-stream",
                        os.path.basename(target))


def action_zip(ctx):
    if not require_admin(ctx):
        return
    rel = ctx.query.get("path", [""])[0]
    target = safe_path(rel)
    if not target or not os.path.isdir(target):
        return ctx.send_html(render("Не найдено", "<div class='card'>Папка не найдена.</div>"), 404)
    name = (os.path.basename(target) or "files") + ".zip"

    # Архив спуливается на диск после SPOOL_MAX, а не собирается в памяти:
    # каталог на несколько гигабайт иначе клал процесс.
    with tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX) as buf:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _dirs, filenames in os.walk(target):
                for fn in filenames:
                    fp = os.path.join(root, fn)
                    # os.walk не заходит в каталоги-симлинки, но файл-симлинк
                    # z.write прочитал бы по назначению — вплоть до /etc/passwd.
                    # Проверяем каждый путь тем же правилом, что и safe_path.
                    if not os.path.realpath(fp).startswith(FILES_ROOT + os.sep):
                        continue
                    z.write(fp, os.path.relpath(fp, target))

        size = buf.tell()
        buf.seek(0)
        ctx.send_stream(buf, size, "application/zip", name)


# --------------------------------------------------------------------------- #
# OIDC / SSO — portal is the OpenID Connect identity provider for CortenDesk
# --------------------------------------------------------------------------- #
_oidc_lock = threading.Lock()
_oidc_codes = {}    # code -> {uid, client_id, redirect_uri, nonce, challenge, exp}
_oidc_tokens = {}   # access_token -> {uid, exp}
_RSA_KEY = None


def _oidc_key():
    global _RSA_KEY
    if _RSA_KEY is not None or not _HAS_JWT:
        return _RSA_KEY
    if os.path.exists(OIDC_KEY_PATH):
        with open(OIDC_KEY_PATH, "rb") as f:
            _RSA_KEY = _ser.load_pem_private_key(f.read(), password=None)
    else:
        _RSA_KEY = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = _RSA_KEY.private_bytes(
            _ser.Encoding.PEM, _ser.PrivateFormat.PKCS8, _ser.NoEncryption()
        )
        with open(OIDC_KEY_PATH, "wb") as f:
            f.write(pem)
        os.chmod(OIDC_KEY_PATH, 0o600)
    return _RSA_KEY


def _b64u_uint(n):
    b = n.to_bytes((n.bit_length() + 7) // 8 or 1, "big")
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _oidc_claims_for(uid):
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        return None
    return {
        "sub": str(u["id"]),
        "email": u["email"],
        "email_verified": True,
        "name": u["email"].split("@")[0],
    }


def _oidc_gc():
    now = time.time()
    with _oidc_lock:
        for d in (_oidc_codes, _oidc_tokens):
            for k in [k for k, v in d.items() if v["exp"] < now]:
                d.pop(k, None)


def oidc_discovery(ctx):
    doc = {
        "issuer": OIDC_ISSUER,
        "authorization_endpoint": OIDC_ISSUER + "/oidc/authorize",
        "token_endpoint": OIDC_ISSUER + "/oidc/token",
        "jwks_uri": OIDC_ISSUER + "/oidc/jwks",
        "userinfo_endpoint": OIDC_ISSUER + "/oidc/userinfo",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": ["openid", "email", "profile"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        "code_challenge_methods_supported": ["S256"],
        "claims_supported": ["sub", "email", "email_verified", "name", "iss", "aud", "exp", "iat", "nonce"],
    }
    ctx.send_json(doc)


def oidc_jwks(ctx):
    key = _oidc_key()
    pub = key.public_key().public_numbers()
    ctx.send_json({"keys": [{
        "kty": "RSA", "use": "sig", "alg": "RS256", "kid": OIDC_KID,
        "n": _b64u_uint(pub.n), "e": _b64u_uint(pub.e),
    }]})


def _oidc_err_redirect(ctx, redirect_uri, state, err, desc=""):
    # Сюда попадают только адреса из OIDC_REDIRECT_URIS, но заголовок Location
    # собирается вручную, а send_header не фильтрует CR/LF — страхуемся.
    if not redirect_uri or any(ch in redirect_uri for ch in "\r\n"):
        return ctx.send_html(
            render("Ошибка", "<div class='card'><h1>400</h1>"
                             "<p class='muted'>Некорректный redirect_uri.</p></div>"), 400)

    sep = "&" if "?" in redirect_uri else "?"
    loc = f"{redirect_uri}{sep}error={quote(err)}"
    if desc:
        loc += "&error_description=" + quote(desc)
    if state:
        loc += "&state=" + quote(state)
    ctx.redirect(loc)


def oidc_authorize(ctx):
    if not OIDC_ENABLED:
        return ctx.send_html(render("SSO выключен", "<div class='card'>SSO не настроен на сервере.</div>"), 404)
    q = ctx.query

    def first(k):
        return q.get(k, [""])[0]

    client_id, redirect_uri = first("client_id"), first("redirect_uri")
    response_type, state, nonce = first("response_type"), first("state"), first("nonce")
    challenge, method = first("code_challenge"), first("code_challenge_method")

    # Пустой OIDC_REDIRECT_URIS раньше означал «пропускать любой redirect_uri»:
    # одна опечатка в юните превращала портал в открытый редиректор, а сырой
    # redirect_uri в заголовке Location — ещё и в расщепление ответа.
    # Теперь список обязателен, а совпадение — точное.
    if client_id != OIDC_CLIENT_ID or redirect_uri not in OIDC_REDIRECT_URIS:
        return ctx.send_html(render("Ошибка SSO",
            "<div class='card'><h1>Ошибка SSO</h1><p class='muted'>Неизвестный client_id или redirect_uri.</p></div>"), 400)
    if response_type != "code":
        return _oidc_err_redirect(ctx, redirect_uri, state, "unsupported_response_type")
    if method and method != "S256":
        return _oidc_err_redirect(ctx, redirect_uri, state, "invalid_request", "only S256 supported")

    if not ctx.user:
        # Not signed in to the portal — remember where to return, then show login.
        ctx.set_cookie(f"post_login={quote(ctx.raw_path)}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=600")
        return ctx.redirect("/")

    code = secrets.token_urlsafe(32)
    with _oidc_lock:
        _oidc_codes[code] = {
            "uid": ctx.user["id"], "client_id": client_id, "redirect_uri": redirect_uri,
            "nonce": nonce, "challenge": challenge, "exp": time.time() + 300,
        }
    sep = "&" if "?" in redirect_uri else "?"
    loc = f"{redirect_uri}{sep}code={quote(code)}"
    if state:
        loc += "&state=" + quote(state)
    ctx.redirect(loc)


def _oidc_client_auth(ctx):
    cid = ctx.form.get("client_id", "")
    csec = ctx.form.get("client_secret", "")
    auth = ctx.h.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            dec = base64.b64decode(auth[6:]).decode()
            bcid, bsec = dec.split(":", 1)
            cid = cid or unquote(bcid)
            csec = csec or unquote(bsec)
        except Exception:
            pass
    return cid, csec


def oidc_token(ctx):
    if not OIDC_ENABLED:
        return ctx.send_json({"error": "temporarily_unavailable"}, 503)
    _oidc_gc()
    cid, csec = _oidc_client_auth(ctx)
    if not hmac.compare_digest(cid, OIDC_CLIENT_ID) or not hmac.compare_digest(csec, OIDC_CLIENT_SECRET):
        return ctx.send_json({"error": "invalid_client"}, 401)
    if ctx.form.get("grant_type") != "authorization_code":
        return ctx.send_json({"error": "unsupported_grant_type"}, 400)
    code = ctx.form.get("code", "")
    redirect_uri = ctx.form.get("redirect_uri", "")
    verifier = ctx.form.get("code_verifier", "")
    with _oidc_lock:
        rec = _oidc_codes.pop(code, None)
    if not rec or rec["exp"] < time.time() or rec["redirect_uri"] != redirect_uri or rec["client_id"] != cid:
        return ctx.send_json({"error": "invalid_grant"}, 400)
    if rec["challenge"]:
        calc = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        if not hmac.compare_digest(calc, rec["challenge"]):
            return ctx.send_json({"error": "invalid_grant", "error_description": "PKCE failed"}, 400)
    base = _oidc_claims_for(rec["uid"])
    if not base:
        return ctx.send_json({"error": "invalid_grant"}, 400)
    now = int(time.time())
    claims = {"iss": OIDC_ISSUER, "aud": cid, "iat": now, "exp": now + 3600, "auth_time": now, **base}
    if rec["nonce"]:
        claims["nonce"] = rec["nonce"]
    id_token = _jwt.encode(claims, _oidc_key(), algorithm="RS256", headers={"kid": OIDC_KID})
    access_token = secrets.token_urlsafe(32)
    with _oidc_lock:
        _oidc_tokens[access_token] = {"uid": rec["uid"], "exp": now + 3600}
    ctx.send_json({
        "access_token": access_token, "token_type": "Bearer", "expires_in": 3600,
        "id_token": id_token, "scope": "openid email profile",
    })


def oidc_userinfo(ctx):
    if not OIDC_ENABLED:
        return ctx.send_json({"error": "temporarily_unavailable"}, 503)
    auth = ctx.h.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        ctx.h.send_response(401)
        ctx.h.send_header("WWW-Authenticate", "Bearer")
        ctx.h.end_headers()
        return
    with _oidc_lock:
        rec = _oidc_tokens.get(auth[7:].strip())
    if not rec or rec["exp"] < time.time():
        return ctx.send_json({"error": "invalid_token"}, 401)
    base = _oidc_claims_for(rec["uid"])
    if not base:
        return ctx.send_json({"error": "invalid_token"}, 401)
    ctx.send_json(base)


def post_login_redirect(ctx):
    """After a successful portal login/register, resume a pending SSO flow."""
    nxt = unquote(ctx.cookies.get("post_login", ""))
    if nxt.startswith("/oidc/authorize"):
        ctx.set_cookie("post_login=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0")
        return ctx.redirect(nxt)
    ctx.redirect("/dashboard")


# --------------------------------------------------------------------------- #
# Auth guards
# --------------------------------------------------------------------------- #
def require_login(ctx):
    if not ctx.user:
        ctx.redirect("/")
        return None
    return ctx.user


def require_admin(ctx):
    if not ctx.user:
        ctx.redirect("/")
        return None
    if ctx.user["role"] != "admin":
        ctx.send_html(render("Доступ запрещён", "<div class='card'><h1>403</h1>"
                             "<p class='muted'>Только для администраторов.</p></div>", user=ctx.user), 403)
        return None
    return ctx.user


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
GET_ROUTES = {
    "/": view_index,
    "/dashboard": view_dashboard,
    "/admin": view_admin,
    "/files": view_files,
    "/files/download": action_download,
    "/device/windows": action_windows_setup,
    "/files/zip": action_zip,
    "/logout": action_logout,
    "/.well-known/openid-configuration": oidc_discovery,
    "/oidc/jwks": oidc_jwks,
    "/oidc/authorize": oidc_authorize,
    "/oidc/userinfo": oidc_userinfo,
}
POST_ROUTES = {
    "/register": action_register,
    "/login": action_login,
    "/device": action_device,
    "/admin/action": action_admin,
    "/admin/create": action_admin_create,
    "/files/upload": action_upload,
    "/files/mkdir": action_mkdir,
    "/files/delete": action_delete,
    "/api/heartbeat": api_heartbeat,
    "/oidc/token": oidc_token,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "RemotePortal/1.0"
    # BaseHTTPRequestHandler читает у timeout значение для сокета: без него
    # соединение, отдающее заголовки по байту в минуту, держит поток вечно.
    timeout = SOCKET_TIMEOUT

    def log_message(self, fmt, *args):
        pass  # keep console quiet

    def _dispatch(self, routes, read_body=False):
        ctx = Ctx(self)
        fn = routes.get(ctx.path)

        if not fn:
            # Раньше тело читалось до этой строки, поэтому POST на любой
            # несуществующий путь позволял анонимно набивать память или /tmp.
            ctx.send_html(render("Не найдено", "<div class='card'><h1>404 — Не найдено</h1></div>", user=ctx.user), 404)
            ctx.close_files()
            return

        if read_body:
            limit = MAX_UPLOAD_BODY if ctx.path in LARGE_BODY_ROUTES else MAX_BODY
            try:
                declared = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                declared = -1

            if declared < 0 or declared > limit:
                ctx.send_html(
                    render("Слишком большой запрос",
                           "<div class='card'><h1>413</h1>"
                           "<p class='muted'>Тело запроса превышает допустимый размер.</p></div>"),
                    413,
                )
                return

            ctx.read_body()

        try:
            fn(ctx)
        except BrokenPipeError:
            pass
        except Exception:  # pragma: no cover
            # Наружу — только общий текст: сообщение исключения выдаёт абсолютные
            # пути, куски SQL и структуру кода. Подробности уходят в journalctl.
            traceback.print_exc()
            try:
                ctx.send_html(
                    render("Ошибка",
                           "<div class='card'><h1>500</h1>"
                           "<p class='muted'>Внутренняя ошибка. Подробности записаны в журнал сервера.</p></div>"),
                    500,
                )
            except Exception:
                pass
        finally:
            ctx.close_files()

    def do_GET(self):
        self._dispatch(GET_ROUTES)

    def do_POST(self):
        self._dispatch(POST_ROUTES, read_body=True)


def main():
    init_db()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Remote-Access Portal running on http://{HOST}:{PORT}")
    print(f"  DB:          {DB_PATH}")
    print(f"  Files root:  {FILES_ROOT}")
    print(f"  CortenDesk:  {CORTENDESK_URL}")
    print("  First registered account becomes the administrator.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        srv.shutdown()


if __name__ == "__main__":
    main()
