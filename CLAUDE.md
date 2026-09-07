# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`webportal/` is a **Remote-Access Portal**: a single-file Python web app (`app.py`, ~1200 lines,
**standard library only** apart from the optional OIDC dependencies) that sits in front of a
**CortenDesk / RustDesk** remote-desktop server. It owns accounts, roles, remote-access
permissions, presence, an admin file manager, and acts as the **OpenID Connect identity
provider** that CortenDesk logs users in against.

This directory is **not a git repository** — there is no history to consult and no commits to make
unless the user asks to `git init`. `webportal/app.py.bak` is a manual backup, not a tracked file.

## This host is the live deployment

The code here is not a checkout — it *is* what production runs. Editing `app.py` changes the
running service after a restart, so treat edits accordingly.

| Piece | Where |
|---|---|
| Portal process | `systemd` unit `remote-portal.service`, runs `.venv/bin/python app.py` on `127.0.0.1:8000` |
| Real configuration | `Environment=` lines in `/etc/systemd/system/remote-portal.service` — **not** `run.sh` |
| Public entry | Caddy (`/etc/caddy/Caddyfile`): `spritelutsk.duckdns.org` → portal :8000, `rd.spritelutsk.duckdns.org` → CortenDesk :8080 |
| Sandbox root | `/home/sprite_lutsk1/remote/` (`FILES_ROOT`) — весь каталог проекта, включая `.secret`, `portal.db`, ключ OIDC |

```bash
sudo systemctl restart remote-portal.service    # apply an app.py change
systemctl status remote-portal.service
journalctl -u remote-portal.service -n 50       # only startup banner + tracebacks; requests are not logged
```

`Handler.log_message` is a no-op, so nothing appears per request — add a print or watch the
response codes instead when debugging.

## Running and testing

There is **no test suite, linter, or build step**. Verification is done by exercising the running
server with `curl`.

```bash
python3 -c "import ast;ast.parse(open('webportal/app.py',encoding='utf-8').read())"   # syntax check
```

Beware `run.sh`: it launches with the system `python3`, which lacks `PyJWT`/`cryptography`, so
`_HAS_JWT` is false and **all OIDC/SSO silently turns off** (`OIDC_ENABLED`). Use
`.venv/bin/python app.py` for anything touching SSO, and note it will fight the systemd instance
for port 8000 — stop the service first or set `PORT=`.

Sessions and CSRF are pure HMAC over `SECRET_KEY`, so a test session can be minted offline
without logging in — this is the fastest way to smoke-test authenticated routes:

```python
# SECRET_KEY comes from the systemd unit; uid from `select id,role from users` in portal.db
session = base64url(json.dumps({"uid": UID, "exp": now + 3600})) + "." + hmac_sha256(SECRET_KEY, raw)[:32]
# CSRF is bound to the session cookie itself and expires after CSRF_TTL (12h),
# so it is computed over the assembled session string, not the uid alone:
issued  = now
csrf    = f"{issued}." + hmac_sha256(SECRET_KEY, f"csrf:{UID}:{session}:{issued}")[:32]
```

Then e.g. `curl -X POST 'http://127.0.0.1:8000/files/upload?path=' -H "Cookie: session=$C" -F "csrf=$CS" -F 'file=@x.txt'`.
Every POST route requires the `csrf` form field (checked by `csrf_valid`, which verifies
format, age and signature via `compare_digest`); `/files*` and `/admin*` require
`role='admin'`. A token minted for one session is rejected by another, and re-logging in
invalidates every token issued before.

## Architecture

**Request lifecycle.** `Handler.do_GET`/`do_POST` → `_dispatch` builds a `Ctx` (parses cookies,
loads `ctx.user` from the signed session, parses the body for POST) → looks the path up in the
flat `GET_ROUTES` / `POST_ROUTES` dicts → calls the handler, which writes the response itself via
`ctx.send_html` / `ctx.redirect` / `ctx.send_json`. Handlers return nothing meaningful; guards
(`require_login`, `require_admin`) emit their own redirect/403 and return `None`, so the caller
must bail on a falsy user. `ThreadingHTTPServer` means one thread per request over one SQLite file.

**HTML.** No templates and no client framework. Pages are f-strings assembled inside view
functions and wrapped by `render(title, body, user, flash, script)` into the single `PAGE`
constant (inline CSS, light theme). **All interpolated data must go through `e()`** — it is the only
escaping there is. UI text is Russian; keep new strings Russian to match.

**State passing.** Flash messages travel through the redirect URL (`?ok=`/`?err=`), built by
`redirect_flash` and read back by `flash_from_query` — there is no server-side flash storage.

**Data.** One SQLite table `users` (role, `remote_allowed`, `rustdesk_id`, `last_seen`). The first
account registered becomes admin — and that is the *only* case self-service registration is
open by default: `registration_open()` returns true for an empty `users` table or when
`ALLOW_REGISTRATION=true`, otherwise `/register` is refused and the tab is hidden.
Accounts are added instead from the admin panel — `POST /admin/create`
(`action_admin_create`), which shares `EMAIL_RE` / `MIN_PASSWORD_LEN` with registration.
"Online" = `last_seen` within `ONLINE_WINDOW` (60s), refreshed by the dashboard POSTing
`/api/heartbeat` every 20s — portal presence, not RustDesk device liveness.

**File manager** (`/files`, admin only). Everything is confined by `safe_path()`, which resolves
against `FILES_ROOT` with `realpath` and rejects anything escaping it — every new path-taking
handler must go through it. Uploads are parsed by the hand-written streaming multipart parser in
`Ctx._parse_multipart`: it reads the body in `MULTIPART_CHUNK` slices and spools parts over
`SPOOL_MAX` to disk, so **never buffer the whole request body**. `ctx.files` is a *list* of
`(field, filename, fileobj)` — a list, not a dict, because `<input multiple>` sends every file
under the same field name. `_dispatch` closes those objects in a `finally`.

**OIDC / SSO** (`_HAS_JWT` gated, `/.well-known/openid-configuration`, `/oidc/authorize|token|jwks|userinfo`).
Authorization code + PKCE, RS256, key persisted at `oidc_rsa_key.pem`. Auth codes and access
tokens live in the module-level dicts `_oidc_codes` / `_oidc_tokens` under `_oidc_lock` — **in
memory only**, so a restart invalidates in-flight logins, and the design assumes a single process
(do not add workers). An unauthenticated `/oidc/authorize` stashes the flow in a `post_login`
cookie that `post_login_redirect` resumes after login. Caddy rewrites a bare `GET /login` on the
CortenDesk vhost to `/login/oidc` for click-free SSO. Break-glass: CortenDesk's local password
login is deliberately left enabled; `CORTENDESK_OIDC_DISABLED=true` on its container bypasses SSO.

## Secrets living in the tree

`.secret` (session/CSRF HMAC key, also duplicated into the systemd unit), `.oidc_secret`,
`oidc_rsa_key.pem`, and `portal.db` (password hashes) sit next to the code. Changing `SECRET_KEY`
logs everyone out and invalidates every outstanding CSRF token. Exclude these when packaging or
sharing the directory.

## Reference

`webportal/README.md` documents the env-var table, the CortenDesk container invocation, the
RustDesk ID wiring, and the SSO/desktop-client notes (its later sections are in Russian).

---

An OpenAI Codex directory exists at `~/.codex/` (binary packages only, no `config.toml` found).
Reply `/import` if you want Claude Code to scan it for importable settings.
