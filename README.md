# Registry API

An API for managing nginx HTTP reverse proxies and TCP/UDP stream proxies.
The backend stores domains, routes, configuration versions, and certificate metadata
in a database, generates nginx configurations, and applies them with validation and rollback.

**Aiohttp serves only the API and `/health`.** It does not read Vue dist, serve
`index.html`, assets, or SPA routes. Docker does not build or include Vue either.
The previous interface sources remain in `registry/` for separate reworking and publishing.

## Structure

- `main.py`, `backend/` — aiohttp API, async SQLAlchemy, and Mako.
- `alembic/` — SQLite/MySQL migrations.
- `registry/` — independent Vue 3 frontend.
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

The image includes the Python API, nginx, certbot, and openssl. Node/Vue are absent.
The local example runs nginx inside the container and uses its own named volumes.

```powershell
$env:CERTBOT_EMAIL = "operator@example.com"
docker compose -f docker-compose.example.yml up -d --build
```

The API is published on localhost:8090; nginx uses ports 80 and 443.
Publish stream ports separately, adding `/udp` for UDP.
Do not run the example on ports already occupied by host nginx.
To manage host nginx, run the API on the host with its paths and commands:
validation and reload must target the same nginx instance that serves traffic.

Inside the container, entrypoint supervises the API and nginx: either process exiting
stops the container, and the restart policy starts it again. Startup runs database
migrations. The healthcheck uses `/health`.
The panel is intended for trusted operators; use TLS for remote access.

### CI/CD and production

`docker-compose.yml` is the production configuration. It pulls a published image
specified by `REGISTRY_IMAGE`, with no local build. Its fixed Compose project name is
`registry`, keeping production volumes stable across deployment directories.

Changes merged into `master` publish `latest` and `sha-<full-commit-sha>` to Docker Hub.
Once checks and publishing succeed, advance `deploy` to that exact commit without
creating a new merge commit. A push to `deploy` deploys the existing image to the VPS
by digest and waits for container health. Deployment does not build images or repeat tests.
The Vue frontend remains a separate deployment.

See [the workflow guide](.github/workflows/README.md) for all triggers, GitHub secrets
and variables, VPS setup, first deployment, version promotion, troubleshooting, and rollback.

## Independent Vue frontend

```sh
cd registry
npm ci
npm test
npm run serve
# or
npm run build
```

The development server listens on port 3000 and proxies `/api` to `127.0.0.1:8090`.
`API_PROXY_TARGET` changes the development proxy target.
`VUE_APP_API_BASE_URL` sets the API URL during a separate production build;
the default is `/api`.
Publish `registry/dist/` separately; the backend does not use it.

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
