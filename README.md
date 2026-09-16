# Registry

An API for managing nginx HTTP reverse proxies and TCP/UDP stream proxies.
The backend stores domains, routes, configuration versions, and certificate metadata
in a database, generates nginx configurations, and applies them with validation and rollback.

Aiohttp serves the API, `/health`, and the production Vue frontend on the same
`APP_PORT`. Docker builds Vue in a separate Node.js stage and copies only its
compiled files into the Python image. Node.js and `node_modules` are excluded
from the final image. Without a frontend build in `frontend/`, the backend
continues to run as an API-only service.

## Structure

- `main.py`, `backend/` — aiohttp API, async SQLAlchemy, and Mako.
- `alembic/` — SQLite/MySQL migrations.
- `registry/` — Vue 3 frontend sources.
- `docker/` — nginx configuration for proxied traffic.
- `tests/` — unit/API tests and integration checks against real nginx in Linux CI.
- `.github/workflows/` — checks, Docker Hub publishing, and VPS deployment.

## Local backend

Python 3.14+ and uv are required. All local commands and paths are relative to the project root.

```powershell
Copy-Item .env.example .env
uv sync --frozen
uv run alembic upgrade head
uv run python main.py
```

Linux:

```sh
cp .env.example .env
uv sync --frozen
uv run alembic upgrade head
uv run python main.py
```

By default, the API listens on `127.0.0.1:8090`, and SQLite is stored in
`backend/data/app.db`. The `.env` file is loaded on all platforms; environment
variables take precedence. The API validates Bearer JWTs using the auth service's JWKS.
A signed `token_use: "access"` claim is required; refresh tokens and older JWTs
without `token_use` are rejected with HTTP 401. To use an older session, refresh
its tokens through auth or sign in again.
Applying configurations requires real nginx and access to its directories.
On Windows without nginx, you can work with the database, API, and drafts;
apply/certificate operations require a configured environment.

## Configuration

Values come from defaults, `backend/config/settings.yaml`, and the environment.
Unknown YAML settings and invalid boolean/numeric values cause an error.

| Setting | Default / purpose |
| --- | --- |
| `DB_URL` | Async SQLAlchemy URL; SQLite under backend/data by default |
| `APP_HOST`, `APP_PORT` | 127.0.0.1, 8090 |
| `NGINX_TEST_COMMAND` | /usr/sbin/nginx -t; mandatory validation |
| `NGINX_RELOAD_COMMAND` | /usr/sbin/nginx -s reload; an empty value disables reload |
| `NGINX_SITES_AVAILABLE`, `NGINX_SITES_ENABLED` | HTTP source/enabled directories |
| `NGINX_STREAMS_AVAILABLE`, `NGINX_STREAMS_ENABLED` | TCP/UDP source/enabled directories |
| `NGINX_BACKUP_DIR` | backend/runtime/backups |
| `NGINX_COMMAND_TIMEOUT`, `CERTBOT_COMMAND_TIMEOUT` | 30 and 300 seconds |
| `CERTBOT_BIN`, `CERTBOT_EMAIL` | Certbot executable and registration email |
| `CERTBOT_WEBROOT` | /var/www/certbot |
| `LETSENCRYPT_DIR` | /etc/letsencrypt |
| `CERTBOT_WORK_DIR`, `CERTBOT_LOGS_DIR` | /var/lib/letsencrypt, /var/log/letsencrypt |
| `CERTBOT_STAGING` | false by default; 0/false/off disable staging |
| `OPENSSL_BIN` | /usr/bin/openssl; reads certificate expiry |
| `IMPORT_NGINX_CONFIGS` | 1; imports new simple snippets at startup |
| `DEBUG` | 0; logs are written to stderr |
| `AUTH_SERVER` | Auth service host; must match the `iss` claim in issued JWTs |
| `AUTH_PROXY_TARGET` | Optional HTTP/HTTPS auth origin for login and refresh; defaults to `https://AUTH_SERVER` |
| `NOT_SECURED_PATHS` | Public paths in YAML; `/health` is accessible without JWT |

`/health` checks that the domains table is accessible.

## Applying changes and rollback

Creating or editing a rule saves a draft without changing the running nginx configuration.
The API returns `latest_deployment` and `applied_deployment`; versions are selected by ID,
so changes made within the same second remain distinct. `ssl_enabled` describes the
applied TLS configuration, rather than whether a certificate has ever been issued.

All changes are serialized through one DomainService. During apply, separate copies
of each affected file and symlink metadata are saved; source and enabled files are
written atomically. Obsolete paths are removed after a rename or type change.
Then nginx validation, reload, and the database commit run.
If a write, command, or commit fails, the previous files are restored and the previous
configuration is validated and reloaded. Disable/delete operations use the same mechanism.
Failure of the rollback itself is reported explicitly; the server does not report such
an operation as successful.

Backups are stored in separate UUID directories with a manifest.json.
Do not delete pending/rollback_failed manifests without checking the database and nginx state.
Automatic recovery after a process crash is not implemented.
Use one API process and one replica per nginx instance.

Changing an HTTPS domain preserves TLS and the previous certificate name. If new aliases
are not covered by the certificate, apply is rejected: call `/certificate` first.
The service checks occupied aliases, TCP/UDP listen ports, and files owned by other domains.

## Certificates

HTTP-01 through certbot webroot is supported for hostname domains.
The challenge deployment preserves existing HTTPS and serves the ACME location without redirecting it.
If certbot or the final TLS apply fails, the previous configuration is restored.
Successful issuance saves coverage, paths, and expiry.

Staging issues test certificates. Use `CERTBOT_STAGING=0` for real certificates.
DNS must point to this nginx instance, and TCP port 80 must be reachable externally.
Run renewals through an external service timer/scheduler, for example inside the container:

```sh
docker compose -f docker-compose.example.yml exec registry certbot renew \
  --webroot -w /var/www/certbot \
  --deploy-hook '/usr/sbin/nginx -t && /usr/sbin/nginx -s reload'
```

The API does not include a renewal scheduler.
Imported certificate metadata is read during the initial import;
after external renewal, another `/certificate` call updates its database metadata.

## Import

The importer reads only enabled/*.conf. Existing domains and drafts are not overwritten.
Each file corresponds to one managed rule.
Simple HTTP/HTTPS snippets with a single proxy_pass in scheme://host:port form
and stream snippets with a single server are supported. Ambiguous multi-server files
and unsupported upstreams are skipped and logged. One failed import does not undo the others.

## Docker

The image includes the Python API, compiled Vue frontend, nginx, certbot, and
openssl. A separate Node.js build stage runs `npm ci` and `npm run build`; only
`dist/` is copied to `/app/frontend` in the Python runtime stage. Vue source maps
are disabled. Node.js, npm, and `node_modules` do not enter the final image.
The local example runs nginx inside the container and uses its own named volumes.

```powershell
$env:CERTBOT_EMAIL = "operator@example.com"
docker compose -f docker-compose.example.yml up -d --build
```

Docker Compose publishes one frontend and API port, configured through `.env`:

```dotenv
APP_PORT=8090
```

`APP_PORT` sets both the API listener inside the container and the published host
port. The default is 8090 when unset or empty. For example, `APP_PORT=8485` makes
the interface and API available at `http://VPS_IP:8485`. Choose an available port without editing
Compose. HTTP/HTTPS nginx listeners are not published by the supplied Compose files.
To manage host nginx, run the API on the host with its paths and commands:
validation and reload must target the same nginx instance that serves traffic.

Inside the container, entrypoint supervises the API and nginx: either process exiting
stops the container, and the restart policy starts it again. Startup runs database
migrations. The healthcheck uses `/health`.
Docker logs rotate at 10 MB per file, keeping three files. nginx writes its logs to
container stdout/stderr so the same rotation applies. Image builds discard uv's
installation cache and remove the build-only uv executable from the final layer.
The panel is intended for trusted operators; use TLS for remote access.

### CI/CD and production

`docker-compose.yml` is the production configuration. It pulls a published image
specified by `REGISTRY_IMAGE`, with no local build. Its fixed Compose project name is
`registry`, keeping production volumes stable across deployment directories.

Changes merged into `master` publish `latest` and `sha-<full-commit-sha>` to Docker Hub.
Once checks and publishing succeed, advance `deploy` to that exact commit without
creating a new merge commit. A push to `deploy` deploys the existing image to the VPS
by digest and waits for container health. Deployment does not build images or repeat tests.
The same image contains the Vue frontend; no separate frontend deployment is required.
After successful startup, deployment removes older local images bearing this
repository's OCI source label, preserving the current image and the previous
version from `.env.previous`. Images referenced by other containers are retained.

For GitHub Actions, set `APP_PORT` in the
`DEPLOY_ENV_FILE` secret. The workflow uploads that file as the VPS `.env` on each
deployment, so changing only the VPS copy will be overwritten on the next run.
An existing host reverse proxy can forward frontend and API requests to `127.0.0.1:APP_PORT`.
The supplied deployment publishes the interface and API; exposing managed nginx traffic and
providing HTTP-01 challenge routing are separate infrastructure tasks.

See [the workflow guide](.github/workflows/README.md) for all triggers, GitHub secrets
and variables, VPS setup, first deployment, version promotion, troubleshooting, and rollback.

## Vue frontend

```sh
cd registry
npm ci
npm test
npm run serve
# or
npm run build
```

The development server listens on port 3000 and proxies `/api` to `127.0.0.1:8081`.
`API_PROXY_TARGET` changes the development proxy target.
The frontend uses same-origin `/api` and `/api/auth` URLs. Development auth
requests default to `https://id.xho.st`; `AUTH_PROXY_TARGET` overrides that origin,
and `AUTH_SERVER` changes the default host.

In Docker, aiohttp serves files from `/app/frontend`. `/static/` holds compiled
resources; public images and the favicon are served from the same frontend
directory. Vue history routes such as `/auth` and `/domains/new` return
`index.html`. Missing assets and unknown API routes remain errors. HTML and
assets are public; domain API endpoints still require a valid access JWT.
`index.html` uses `Cache-Control: no-cache`, and assets are cached for one hour.

For a production build outside Docker, run `npm run build` and copy the contents
of `registry/dist/` to `frontend/` at the repository root before starting Python.

The backend forwards only `POST /api/auth` (with or without a trailing slash)
and `POST /api/auth/refresh` to `/` and `/refresh` on the configured auth origin.
These endpoints do not require an access token. JSON bodies and upstream error
statuses are preserved; browser cookies and Authorization headers are not
forwarded. Responses containing tokens are not cached. Redirects are rejected,
and an unavailable auth service returns 503. The proxy has a 20-second timeout.
`AUTH_PROXY_TARGET` must be an HTTP/HTTPS origin without credentials, a path,
query, or fragment. It can point to an internal auth service while `AUTH_SERVER`
continues to identify the JWT issuer. Compose passes both settings from `.env`.

## API

- GET /health
- GET /api/domains
- GET /api/domains/{id}
- POST /api/domains
- PUT /api/domains/{id}
- POST /api/domains/{id}/apply
- POST /api/domains/{id}/certificate
- POST /api/domains/{id}/disable
- DELETE /api/domains/{id}

Create a hostname:

```json
{
  "type": "hostname",
  "name": "example.com",
  "server_names": ["example.com", "www.example.com"],
  "upstream_host": "127.0.0.1",
  "upstream_port": 3000,
  "upstream_scheme": "http"
}
```

Create a stream:

```json
{
  "type": "port_proxy",
  "name": "minecraft",
  "listen_port": 25565,
  "stream_protocol": "tcp",
  "upstream_host": "10.20.30.10",
  "upstream_port": 25565
}
```

PUT accepts partial updates. IPv6 upstreams are supported.
Success: `{"status":"success","body":{}}`.
Error: `{"status":"error","message":"..."}`.
Status codes: 400 validation, 404 missing domain/route, 409 conflict,
415 content type, 502 nginx/certbot failure, 500 internal failure.
Internal exceptions are logged; their details are not exposed to clients.

## Checks

```sh
uv run python -m unittest discover -s tests -v
uv run alembic upgrade head
uv run alembic check
cd registry
npm test
npm run build
```

Unit/API tests use temporary files and in-memory SQLite.
Linux CI additionally validates configurations against real nginx and openssl.
Migration 7c102e936fe1 adds TLS/coverage metadata and extends deployment status.
The copy does not include .env, databases, certificates, or runtime state from the original project.
For an existing database, create a backup first, then set its DB_URL and
run `alembic upgrade head`.
