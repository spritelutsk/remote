# Remote-Access Portal

A small, self-contained web portal (Python **standard library only** — no pip
installs) that sits in front of a **CortenDesk** / RustDesk remote-desktop
server and manages *who* is allowed to remotely control *whom*.

CortenDesk (https://github.com/marcpope/cortendesk) is the engine that does the
actual screen sharing over the RustDesk protocol. This portal is the front door:
accounts, roles, permissions, live status, and file transfer.

## Features

- **Landing page** with email + password **login** and **registration**.
- Accounts stored in **SQLite** (`portal.db`).
- Roles: **admin** and **user**. The **first** account created becomes admin.
- Per-user **remote-access** flag — an admin can allow/deny remote control of
  each user's desktop.
- **Anyone-to-anyone** connections: a user opens the dashboard, sees which peers
  are online and remote-enabled, and clicks **Connect** to launch the CortenDesk
  web client for that device.
- **Admin control panel**: every user with live online/offline status, role,
  remote-access toggle, device ID, last-seen, delete — and a **create user** form
  (email, password, admin/remote-access flags), which is how accounts are added now
  that self-service registration is off by default.
- **File manager** (admin): browse the current directory, upload / download
  files, create folders, and **download the whole folder as a ZIP** archive.
  Sandboxed to `FILES_ROOT` with path-traversal protection.

## Run

```bash
cd webportal
./run.sh              # or:  python3 app.py
# open http://localhost:8000
```

Register the first account — it becomes the administrator. Register more
accounts as regular users, then use the **Admin** panel to allow remote access.

## Configuration (environment variables)

| Variable               | Default                     | Meaning                                            |
|------------------------|-----------------------------|----------------------------------------------------|
| `PORT`                 | `8000`                      | Listen port                                        |
| `HOST`                 | `0.0.0.0`                   | Bind address                                       |
| `DB_PATH`              | `./portal.db`               | SQLite database file                               |
| `FILES_ROOT`           | `..` (каталог remote)       | Sandbox root for the file manager                  |
| `SECRET_KEY`           | random per run              | Cookie signing key (set a fixed one in production) |
| `CORTENDESK_URL`       | `http://localhost:8080`     | Base URL of your CortenDesk server                 |
| `CONNECT_URL_TEMPLATE` | `{base}/#/connect/{id}`     | How a "Connect" link is built (`{base}`, `{id}`)   |
| `ALLOW_REGISTRATION`   | off                         | Open self-service registration (an empty user table always allows it, so the first admin can still be created) |
| `RUSTDESK_HOST`        | host of `CORTENDESK_URL`    | ID/relay server written into the Windows setup script |
| `RUSTDESK_KEY`         | file `.rustdesk_key`        | Server public key (`id_ed25519.pub`); without it the Windows button is disabled |
| `RUSTDESK_KEY_FILE`    | `./.rustdesk_key`           | Where the key is read from when `RUSTDESK_KEY` is unset |
| `RUSTDESK_VERSION`     | `1.4.9`                     | RustDesk release the setup script downloads        |

`run.sh` auto-generates and persists a `SECRET_KEY` in `.secret` on first run so
sessions survive restarts.

## Connecting the two pieces (CortenDesk)

1. **Run CortenDesk** (its own server, per its README):

   ```bash
   docker run -d --name cortendesk \
     -e APP_URL=https://rd.example.com \
     -p 8080:8080 -p 21115-21119:21115-21119 \
     -v cortendesk-data:/data ghcr.io/marcpope/cortendesk:1.8.1
   ```

2. Point this portal at it: `export CORTENDESK_URL=https://rd.example.com`.

3. Each user installs a **RustDesk client**, configures it to use the CortenDesk
   ID/relay servers, and pastes its **RustDesk ID** into the portal Dashboard
   ("My device"). An admin then **allows remote** for that user.

4. From the Dashboard, other users click **Connect** — the portal opens
   `CONNECT_URL_TEMPLATE` (the CortenDesk web client) for that device ID.
   Adjust `CONNECT_URL_TEMPLATE` to match your CortenDesk build's URL scheme.

## Online status

"Online" means the user's browser sent a heartbeat within the last 60s (the
Dashboard pings `/api/heartbeat` every 20s). It reflects portal presence, not
the RustDesk device's own connection state — swap in a query to CortenDesk's
API if you need true device liveness.

## Security notes

- Passwords hashed with PBKDF2-HMAC-SHA256 (200k iterations, per-user salt).
- Sessions are HMAC-signed HttpOnly cookies; POST forms carry CSRF tokens.
- The file manager is admin-only and sandboxed; still, expose it only on a
  trusted network or behind TLS/reverse proxy — put HTTPS in front for production.
```

## SSO (единый вход) с CortenDesk

Портал работает как **OpenID Connect провайдер** (IdP), CortenDesk — как клиент.
Пользователь входит один раз в портал; на странице входа CortenDesk кнопка
«Войти через портал» отправляет его на портал и, если сессия уже активна,
возвращает обратно уже авторизованным — пароль второй раз не вводится.

- Реализация OIDC (authorization code + PKCE, RS256): эндпоинты
  `/.well-known/openid-configuration`, `/oidc/authorize`, `/oidc/token`,
  `/oidc/jwks`, `/oidc/userinfo` в `app.py`.
- Подпись id_token — RSA-ключ `oidc_rsa_key.pem` (генерируется при первом старте).
- Секрет клиента — в `.oidc_secret`; задаётся порталу через env юнита и CortenDesk
  через `Setting::put('oidc_client_secret', …)`.
- Новые пользователи создаются в CortenDesk автоматически (JIT, политика `active`).
- Требуются зависимости `PyJWT` + `cryptography` из `.venv` (портал запускается
  интерпретатором `.venv/bin/python`).

**Аварийный доступ:** локальный вход в консоль CortenDesk по паролю оставлен
включённым. Если OIDC сломается — поднимите контейнер с
`-e CORTENDESK_OIDC_DISABLED=true`. Чтобы вход был совсем без клика,
включите в CortenDesk `oidc_disable_local_login=1` (тогда пароль-логин в консоль
отключается — держите про запас break-glass).

## Кнопка десктоп-клиента (админка)

В админ-панели у пользователя с заданным RustDesk ID есть кнопка **🖥️ Клиент ▸** —
ссылка `rustdesk://<id>`, которая открывает установленный нативный (консольный)
клиент RustDesk и сразу начинает подключение к этому устройству.
