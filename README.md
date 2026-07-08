# O2Cloud WebDAV Gateway

[![Docker Hub](https://img.shields.io/badge/Docker%20Hub-garanda21%2Fo2cloud__gateway__webdav-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/garanda21/o2cloud_gateway_webdav)

A Docker-first, Linux-native gateway that exposes **O2 Cloud** (the O2 / Telefónica
personal cloud storage service, `cloud.o2online.es`) as a standard **WebDAV** share,
plus a small web **admin panel** for authentication and status.

Point any WebDAV client — Finder, Windows Explorer, `rclone`, Nautilus, mobile
apps — at the gateway and browse, upload, download, move, delete and lock your
O2 Cloud files as if they were a normal network drive. The gateway handles O2's
session cookies, validation keys, upload/download quirks and metadata caching
behind the scenes.

- **WebDAV endpoint:** `/dav`
- **Admin panel:** `/admin`
- **Interactive O2 login:** a real Chromium session running inside the container,
  streamed to your browser over **noVNC** (default port `6080`).

---

## Table of contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Quick start (Docker)](#quick-start-docker)
- [Logging in to O2 (interactive VNC login)](#logging-in-to-o2-interactive-vnc-login)
- [Manual session import](#manual-session-import)
- [Using the WebDAV share](#using-the-webdav-share)
- [Simulated mode (no real O2 account)](#simulated-mode-no-real-o2-account)
- [Secrets](#secrets)
- [Environment variables](#environment-variables)
- [Ports](#ports)
- [Local development](#local-development)
- [Project layout](#project-layout)

---

## How it works

```
WebDAV client ──HTTP──> :8088 /dav ─┐
                                    ├─> o2gateway (FastAPI / ASGI) ─> O2 Cloud API
Browser ──────HTTP──> :8088 /admin ─┘         │
Browser ──────HTTP──> :6080 noVNC ────────────┘ (interactive login only)
```

The gateway is a single FastAPI/ASGI application (served by Uvicorn) that:

1. Serves a **WebDAV router** at `/dav` (PROPFIND, GET, PUT, DELETE, MKCOL, MOVE,
   COPY, LOCK/UNLOCK) backed by a pluggable cloud provider.
2. Serves an **admin panel** at `/admin` for O2 login, session status and logout.
3. Translates WebDAV operations into O2 Cloud API calls, caching metadata in a
   local SQLite database to keep listings fast.
4. Stores the O2 session (cookies + validation key) encrypted on disk.

The container also runs a headless X server (`Xvfb`) + `fluxbox` + `x11vnc` +
`websockify`/`noVNC` so the interactive Chromium login can be driven from your
own browser — see below.

## Requirements

- **Docker** and **Docker Compose** (the supported path — bundles Chromium,
  Playwright and the VNC stack).
- A modern browser to reach `/admin` and the noVNC login screen.
- An O2 Cloud account (unless you only run the `simulated` provider).

For local (non-Docker) development you need Python **3.12+**.

## Quick start (Docker)

```bash
# 1. Configuration
cp .env.example .env

# 2. Create host directories mounted into the container
mkdir -p secrets config cache data

# 3. Create the secret files (see the Secrets section)
printf 'change-me-webdav\n' > secrets/webdav_password.txt
printf 'change-me-admin\n'  > secrets/admin_password.txt
openssl rand -base64 32     > secrets/app_encryption_key.txt

# 4. Start
docker compose up -d
```

By default `docker-compose.yml` pulls the prebuilt image
[`garanda21/o2cloud_gateway_webdav:latest`](https://hub.docker.com/r/garanda21/o2cloud_gateway_webdav)
from Docker Hub — no local build needed. To build from source instead, replace the
`image:` line with `build: .` and run `docker compose up --build`.

Then open:

- Admin panel: <http://localhost:8088/admin>
- WebDAV root: <http://localhost:8088/dav>

The shipped `docker-compose.yml` sets `CLOUD_PROVIDER=o2` (real O2 Cloud). To try
it without an account first, set `CLOUD_PROVIDER=simulated` (see
[Simulated mode](#simulated-mode-no-real-o2-account)).

Log in to the admin panel with `ADMIN_USERNAME` / the admin password, then
authenticate to O2.

## Logging in to O2 (interactive VNC login)

O2 Cloud has no public API token flow, so the gateway logs in the same way a
human does: it drives a **real Chromium browser inside the container** using
Playwright, and lets you complete the login form (including any 2FA / captcha /
consent screens) with your own eyes and hands.

Because the container has no physical display, that Chromium instance runs on a
virtual X server (`Xvfb` on `DISPLAY=:99`) and is exposed to you as a **web-based
VNC session**:

```
Xvfb (:99)  ──>  Chromium (Playwright)  ──>  x11vnc  ──>  websockify + noVNC  ──>  your browser :6080
```

Flow:

1. Open the admin panel at `/admin` and start **Assisted O2 login**. This calls
   `POST /api/admin/o2/login`, which launches Chromium on the virtual display and
   navigates to `O2_LOGIN_URL`.
2. The panel shows (or links to) the **noVNC** screen. By default that is
   `http://localhost:6080/vnc.html?autoconnect=true&resize=scale&reconnect=true`
   (host/port derived from `APP_BASE_URL` + `NOVNC_PORT`, or overridden with
   `O2_LOGIN_NOVNC_URL`).
3. Complete the O2 login **in the VNC window** — type your credentials, pass any
   2FA/captcha, accept consent dialogs.
4. Once you land on the logged-in cloud page, the gateway captures the session
   cookies and the O2 `validationKey`, encrypts them and stores them at
   `O2_SESSION_FILE`. Chromium is then closed.
5. Use **Session status / test / logout** in the admin panel to verify or clear
   the session.

Notes:

- `O2_PLAYWRIGHT_HEADLESS=false` is required for the interactive flow — the
  browser must be rendered so you can see and drive it over VNC. Setting it to
  `true` disables the visible session.
- The bundled `x11vnc` runs with `-nopw` (no VNC password) and is intended to be
  reached only over your own trusted network / port mapping. Do **not** expose
  port `6080` to the public internet.

## Manual session import

If you cannot use the VNC flow (headless host, restricted network), you can import
a session captured elsewhere. Provide the same JSON shape the gateway stores:

```json
{
  "validationKey": "...",
  "cookies": [
    {"name": "...", "value": "...", "domain": "cloud.o2online.es", "path": "/"}
  ],
  "userAgent": "Mozilla/5.0 ...",
  "createdAt": "2026-07-04T12:00:00Z"
}
```

Import it through the admin panel's manual import action (or place it at
`O2_SESSION_FILE`). The gateway validates and encrypts it the same way as the
assisted flow.

## Using the WebDAV share

Authenticate with `WEBDAV_USERNAME` and the WebDAV password.

- **macOS (Finder):** Go → Connect to Server → `http://localhost:8088/dav`
- **Windows:** Map network drive → `http://localhost:8088/dav`
- **rclone:**
  ```bash
  rclone config create o2 webdav \
    url=http://localhost:8088/dav vendor=other \
    user=o2dav pass="$(rclone obscure change-me-webdav)"
  rclone ls o2:
  ```

Set `WEBDAV_READ_ONLY=true` to block all writes (PUT/DELETE/MOVE/MKCOL). LOCK
support is implemented for clients that require it (macOS Finder, Office).

## Simulated mode (no real O2 account)

Set `CLOUD_PROVIDER=simulated` to run the whole WebDAV/admin stack against a fake
in-container filesystem rooted at `SIMULATED_ROOT` (default `/data/simulated`).
No O2 login is needed. Useful for testing WebDAV clients and the gateway itself.

## Secrets

Sensitive values are read from files (Docker secrets), not inline env vars where
possible. Each `*_FILE` variable points at a file whose contents are the secret;
the corresponding non-file variable (if set) takes precedence.

| File (host)                       | Mounted as                        | Purpose                                                        |
|-----------------------------------|-----------------------------------|----------------------------------------------------------------|
| `secrets/webdav_password.txt`     | `/run/secrets/webdav_password`    | WebDAV client password (`WEBDAV_PASSWORD_FILE`)                |
| `secrets/admin_password.txt`      | `/run/secrets/admin_password`     | Admin panel password (`ADMIN_PASSWORD_FILE`)                   |
| `secrets/app_encryption_key.txt`  | `/run/secrets/app_encryption_key` | Encrypts the stored O2 session and signs admin sessions (`APP_ENCRYPTION_KEY_FILE`, `ADMIN_SESSION_SECRET_FILE`) |

Generate the encryption key with `openssl rand -base64 32`. Keep these files out
of version control (`secrets/` should be git-ignored).

## Environment variables

All settings come from environment variables (or `.env`). Defaults below are the
in-code defaults; `docker-compose.yml` overrides several of them.

### Application

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_HOST` | `0.0.0.0` | Interface the ASGI server binds to inside the container. |
| `APP_PORT` | `8080` | Port the app listens on inside the container (mapped to `8088` on the host by compose). |
| `APP_BASE_URL` | `http://localhost:8080` | Public base URL of the gateway. Used to build absolute links, including the default noVNC URL. |
| `APP_ENCRYPTION_KEY_FILE` | _(unset)_ | File containing the encryption key used to encrypt the stored O2 session. |
| `PUID` | `10001` | Host user id the container runs as. Set to the owner of the mounted volumes so the app can write `/config`, `/cache`, `/data`. |
| `PGID` | `10001` | Host group id the container runs as (see `PUID`). |

The container starts as root only to apply `PUID`/`PGID`, fix volume ownership and
prepare the X11 socket, then drops privileges via `gosu`. No `user:` override or
`HOME` variable is needed in compose.

### Cloud provider

| Variable | Default | Description |
|----------|---------|-------------|
| `CLOUD_PROVIDER` | `simulated` | Backend to serve: `o2` (O2 Cloud), `movistar` (Movistar Cloud), or `simulated` (fake local filesystem). |
| `SIMULATED_ROOT` | `/data/simulated` | Root directory for the simulated provider's fake files. |

### O2 Cloud

| Variable | Default | Description |
|----------|---------|-------------|
| `O2_API_BASE_URL` | `https://cloud.o2online.es/sapi/` | Base URL for O2 Cloud metadata/API calls. |
| `O2_UPLOAD_BASE_URL` | `https://upload.cloud.o2online.es/sapi/` | Base URL for O2 Cloud uploads (separate host). |
| `O2_LOGIN_URL` | `https://cloud.o2online.es/` | Page Chromium opens for the interactive login. |
| `O2_SESSION_FILE` | `/config/secrets/o2-session.json` | Where the encrypted O2 session (cookies + validation key) is stored. |
| `O2_PLAYWRIGHT_HEADLESS` | `false` | Run the login Chromium headless. Must be `false` for the interactive VNC login to be visible. |
| `O2_HTTP_TIMEOUT_SECONDS` | `120` | Timeout for O2 API HTTP requests. |

For Movistar Cloud, setting the provider is enough. The gateway automatically
uses the Movistar API, upload, and login URLs unless you explicitly set custom
URL values:

```env
CLOUD_PROVIDER=movistar
```

### Interactive login / VNC

| Variable | Default | Description |
|----------|---------|-------------|
| `O2_LOGIN_NOVNC_URL` | _(unset)_ | Explicit URL to the noVNC login screen. If unset, it is derived from `APP_BASE_URL`, `NOVNC_PORT` and `NOVNC_PATH`. **Set this whenever the published host port differs from the internal `6080`** (e.g. you remapped it because `6080` was taken), or the gateway is reached by IP/hostname — otherwise the login link points at the wrong port. |
| `NOVNC_PORT` | `6080` | Host/container port serving the noVNC web client. |
| `NOVNC_PATH` | `/vnc.html?autoconnect=true&resize=scale&reconnect=true` | Path + query appended when building the noVNC URL. |
| `DISPLAY` | `:99` | Virtual X display Chromium and VNC use (set in Dockerfile/compose). |
| `XVFB_SCREEN` | `1280x900x24` | Virtual framebuffer geometry for the login browser. |
| `VNC_PORT` | `5900` | Internal RFB port `x11vnc` listens on (proxied by websockify). |
| `NOVNC_HOST` | `0.0.0.0` | Interface websockify binds the noVNC web server to. |

### WebDAV

| Variable | Default | Description |
|----------|---------|-------------|
| `WEBDAV_ENABLED` | `true` | Enable the `/dav` WebDAV router. |
| `WEBDAV_PATH_BASE` | `/dav` | URL path prefix the WebDAV share is mounted at. |
| `WEBDAV_USERNAME` | `o2dav` | Username WebDAV clients authenticate with. |
| `WEBDAV_PASSWORD` | _(unset)_ | Inline WebDAV password. Prefer `WEBDAV_PASSWORD_FILE`. |
| `WEBDAV_PASSWORD_FILE` | `/run/secrets/webdav_password` | File containing the WebDAV password. |
| `WEBDAV_READ_ONLY` | `false` | When `true`, reject all write methods (PUT/DELETE/MOVE/MKCOL/COPY). |
| `WEBDAV_ALLOW_DOTFILES` | `true` | Allow listing/serving dotfiles (names starting with `.`). |
| `WEBDAV_DEPTH_INFINITY` | `false` | Allow `Depth: infinity` PROPFIND requests (can be expensive). |
| `WEBDAV_IGNORE_APPLEDOUBLE` | `true` | Ignore macOS AppleDouble sidecar files (`._*`) instead of uploading them to the cloud. |

### Admin panel

| Variable | Default | Description |
|----------|---------|-------------|
| `ADMIN_ENABLED` | `true` | Enable the `/admin` panel. |
| `ADMIN_PATH_BASE` | `/admin` | URL path prefix for the admin panel. |
| `ADMIN_USERNAME` | `admin` | Admin login username. |
| `ADMIN_PASSWORD` | _(unset)_ | Inline admin password. Prefer `ADMIN_PASSWORD_FILE`. |
| `ADMIN_PASSWORD_FILE` | `/run/secrets/admin_password` | File containing the admin password. |
| `ADMIN_SESSION_SECRET_FILE` | `/run/secrets/app_encryption_key` | File whose contents sign admin session cookies. |

### Paths & storage

| Variable | Default | Description |
|----------|---------|-------------|
| `CONFIG_DIR` | `/config` | Config directory (mounted volume). |
| `CACHE_DIR` | `/cache` | Cache directory (mounted volume). |
| `DATA_DIR` | `/data` | Data directory (mounted volume). |
| `SQLITE_PATH` | `/data/o2cloud-gateway.db` | SQLite database file for metadata cache and locks. |
| `UPLOAD_SPOOL_DIR` | `/cache/uploads` | Temporary spool directory for in-progress uploads. |

### Cache & limits

| Variable | Default | Description |
|----------|---------|-------------|
| `CACHE_METADATA_TTL_SECONDS` | `20` | How long directory/file metadata is cached before refetching from O2. |
| `CACHE_NEGATIVE_TTL_SECONDS` | `5` | How long "not found" results are cached. |
| `CACHE_MAX_SIZE_MB` | `4096` | Max on-disk cache size in MB. |
| `UPLOAD_MAX_FILE_MB` | `10240` | Max single-file upload size in MB. |
| `DOWNLOAD_TIMEOUT_SECONDS` | `3600` | Timeout for a single file download from O2. |

### Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `INFO` | Log verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `LOG_FILE` | `/data/logs/gateway.log` | Path to the log file. |

## Ports

| Container | Host (compose) | Purpose |
|-----------|----------------|---------|
| `8080` | `8088` | HTTP: WebDAV (`/dav`), admin (`/admin`), health (`/health`). |
| `6080` | `6080` | noVNC web client for the interactive O2 login. |

`GET /health` returns the app health status and backs the Docker `HEALTHCHECK`.

## Local development

Docker is the supported runtime, but you can run the app directly for development.
The interactive VNC login requires the container's X/VNC stack, so use the
`simulated` provider (or manual session import) when running on the host.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[test,login]"

# Run tests
pytest

# Run the app with autoreload
uvicorn o2gateway.main:create_app --factory --reload
```

The source targets Python **3.12+** and intentionally avoids newer syntax where
practical, so local smoke tests can run on older macOS Python builds.

## Project layout

```
src/o2gateway/
  main.py               # ASGI app factory / entrypoint
  settings.py           # All environment-driven settings
  webdav/               # WebDAV router, XML, PROPFIND parsing, locks
  admin/                # Admin panel router + templates
  o2/                   # O2 Cloud API client, session store, Playwright login
  cloud/                # Provider interface + simulated provider
  persistence/          # SQLite DB, metadata cache
  security/             # Auth, session encryption
docker/entrypoint.sh    # Starts Xvfb, fluxbox, x11vnc, websockify, then the app
Dockerfile              # Runtime image (Python + Chromium + VNC stack)
docker-compose.yml      # Reference deployment
```

## License

MIT.
