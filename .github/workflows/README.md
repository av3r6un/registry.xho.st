# GitHub Actions workflows

This repository separates validation, image publishing, and production deployment.
All workflows run on `ubuntu-latest` and grant `GITHUB_TOKEN` only `contents: read`.
No workflow commits code, advances branches, or deploys the Vue frontend.

## Workflow overview

| File / workflow | Trigger | Result | GitHub environment |
| --- | --- | --- | --- |
| [checks.yml](checks.yml) / Checks | Pushes to branches other than `deploy`, tag pushes, and all pull requests | Validates backend, migrations, and frontend | None |
| [publish-dockerhub.yml](publish-dockerhub.yml) / Publish Docker Image | A PR merged into `master`, or manual dispatch | Validates backend and publishes Docker Hub images | `publish` |
| [deploy.yml](deploy.yml) / Deploy | A push to `deploy`, or manual dispatch on `deploy` | Pulls an existing image and starts it on the VPS | `deploy` |

The intended sequence is:

1. Open a PR into `master` and wait for Checks.
2. Merge the PR; wait for Publish Docker Image to finish successfully.
3. Advance `deploy` to the published commit without creating a new commit.
4. Wait for Deploy to finish and verify the application on the VPS.

Checks and publishing are separate workflows. Deploy does not automatically wait
for either one. Configure required status checks on `master` and promote only a
commit whose checks and publishing have succeeded. Deploy independently verifies
that the commit belongs to `master` and that its image exists before contacting the VPS.

## Checks

### Triggers and jobs

`push` excludes the `deploy` branch, so promoting a release to `deploy` does not
start Checks. Other branch pushes and tag pushes still run Checks. `pull_request`
has no branch or path filters. Pushes to a PR source branch may produce both push and PR runs.
The `backend` and `frontend` jobs run independently; both must succeed.

### Backend

The job checks out the triggering revision, sets up Python 3.14 and uv, and runs:

```sh
uv sync --frozen
sudo apt-get update && sudo apt-get install -y nginx libnginx-mod-stream openssl
uv run python -m unittest discover -s tests -v
uv run alembic upgrade head && uv run alembic check
```

Tests receive `NGINX_INTEGRATION_BIN=/usr/sbin/nginx`, enabling real nginx integration
tests that would otherwise be skipped. HTTP/HTTPS integration fixtures remap generated listeners to dynamically
allocated loopback ports; stream fixtures also select available ephemeral ports.
These tests do not require permission to bind the container's HTTP/HTTPS ports 80 and 443.
Migration validation uses a disposable SQLite database at `migration-check.db`
via `DB_URL=sqlite+aiosqlite:///migration-check.db`.
`alembic check` detects model changes that have no matching migration.

### Frontend

The job sets up Node.js 22 with npm caching based on `registry/package-lock.json`.
It runs `npm ci`, `npm test`, and `npm run build` inside `registry/`.
The build is a validation step; its dist files are not uploaded or deployed.
Checks requires no custom secrets or variables.

## Publish Docker Image

### Triggers and checkout

The automatic trigger is `pull_request_target`, limited to PRs targeting `master`
and the `closed` event. The `docker` job runs only if the PR was merged.
Closing an unmerged PR does not publish anything. Checkout uses the PR's
`merge_commit_sha`, so the build uses the merged revision.

A direct push to `master` does **not** trigger publishing. For initial repository
setup or a deliberate direct push, use **Actions → Publish Docker Image → Run workflow**
and select `master`. Manual dispatch checks out the selected ref; the workflow can
publish other refs too, but Deploy accepts only commits already in `master`.
Manual dispatch becomes available once the workflow is present on the default branch.

The job uses the `publish` GitHub environment. Environment approvals, if configured,
apply before the job starts. Treat changes to the publishing workflow as privileged:
it uses Docker Hub credentials to build repository code after merge or manual dispatch.

### Validation and image contents

Python 3.14 and uv install the locked dependencies. Before publishing, the job runs
the backend unit/API suite, `alembic upgrade head`, and `alembic check` against
`sqlite+aiosqlite:///backend/runtime/publish-check.db`.
Unlike Checks, this job does not install host nginx or set `NGINX_INTEGRATION_BIN`;
real nginx integration tests are covered by Checks. It does not rerun frontend checks.

Docker Buildx builds `./Dockerfile` with context `.` for `linux/amd64`, pushes it to
Docker Hub, and uses the GitHub Actions build cache (`type=gha`).
Both triggers can restore the cache. Cache export is enabled only for manual
`workflow_dispatch` runs because `pull_request_target` has read-only cache access
by default. Manual exports use `ignore-error=true`, so an unavailable cache service
does not fail image publishing. Docker build and image push failures still fail the job.
The image contains the API, nginx, certbot, and openssl. It contains no Vue frontend.

### Image names and version identity

The image repository is:

```text
docker.io/<DOCKERHUB_USERNAME>/<DOCKERHUB_REPOSITORY or registry>
```

Every successful build publishes:

- `latest`: a moving tag for the last image published; Deploy does not use it.
- `sha-<full-40-character-commit-sha>`: identifies the checked-out source revision.

The SHA is obtained from `git rev-parse HEAD`, rather than the event context. This
keeps the tag and `org.opencontainers.image.revision` label aligned with the actual
checkout for both PR merges and manual runs. Other OCI labels come from metadata-action.

SHA tags identify source commits, but Docker tags can still be overwritten by a
rebuild. Deploy resolves the selected tag to a registry digest before uploading
configuration and uses `docker.io/...@sha256:...` for that run. Preserve published
SHA tags in Docker Hub so older versions remain available. Images published before
the full-SHA tagging change must be republished before they can use this deployment flow.

## Deploy

### Triggers and preflight

A push to `deploy` starts deployment. Manual dispatch runs only when the selected
branch is `deploy`; selecting another branch skips the job. The job checks out
`github.sha`, the exact revision for that event, with full history.

Before any SSH connection, it:

1. Checks that all required deployment settings are nonempty and validates host,
   user, port, and path formats.
2. Fetches `master` and verifies that the deployment commit is its ancestor.
3. Authenticates to Docker Hub and looks up `sha-<full-commit-sha>`.
4. Resolves the image digest and writes a temporary `deployment.env` containing the
   supplied runtime settings plus the resolved `REGISTRY_IMAGE`.

An unavailable image fails the run before changing the VPS. Wait for publishing to
finish, then rerun the failed run or manually dispatch Deploy on `deploy`.
There is no image build, test rerun, or automatic publication retry in this workflow.

The `deploy-production` concurrency group prevents overlapping deployments.
`cancel-in-progress: false` allows an active deployment to finish; GitHub keeps at
most one pending run, and newer runs may replace older pending runs. This is not a
queue that guarantees every intermediate push is deployed.
The job timeout is 15 minutes.

### VPS operations

The workflow loads the SSH private key through ssh-agent. If `VPS_KNOWN_HOSTS` is
set, it installs those trusted host-key entries. Otherwise, it obtains the host keys
from `VPS_HOST` and `VPS_PORT` using `ssh-keyscan` with a 10-second timeout. An empty
or failed scan stops deployment. SSH and SCP use strict host-key checking and
noninteractive authentication in both modes. Automatic scanning does not authenticate
the server against an independently verified key; set `VPS_KNOWN_HOSTS` to pin its identity.

It creates the dedicated deployment directory with owner-only access, checks Docker
Compose availability, copies `docker-compose.yml` and `deployment.env`, and logs the
VPS user into Docker Hub. The token travels through SSH stdin to `docker login
--password-stdin`; it is not written into the application environment file.
Docker stores login credentials according to the VPS user's Docker configuration.

Inside the deployment directory, it runs:

```sh
chmod 600 deployment.env
docker compose --env-file deployment.env config --quiet
docker compose --env-file deployment.env pull
# Preserve the previous environment only after validation and pull succeed.
if [ -f .env ]; then cp -p .env .env.previous; fi
mv deployment.env .env
docker compose up -d --remove-orphans --wait --wait-timeout 120
```

Compose starts the exact resolved digest. Entrypoint applies migrations, validates
nginx, and starts nginx and the API. The existing Docker healthcheck calls `/health`,
which checks database access. Compose waits up to 120 seconds for health.
This verifies container readiness, but does not test external DNS, TLS, auth, or
every proxy route. The temporary environment file on the runner is removed even if
an earlier step fails.

After the health wait succeeds, a separate cleanup step removes older local images
with `org.opencontainers.image.source` matching this GitHub repository. It keeps
the current image and the image referenced by `.env.previous`, when present locally.
Removal does not use force, so images referenced by other containers remain intact.
Cleanup does not remove volumes, containers, or networks. Its failures are non-fatal,
and it prints `docker system df` for storage visibility.

The production and example Compose files use rotating local logs: three files of
10 MB each, with the driver's default compression. nginx writes to stdout/stderr,
so its logs share this limit. The Dockerfile avoids persisting uv's install cache
and removes the build-only uv package in the same layer as dependency installation.

There is no automatic rollback. An unsuccessful startup fails the workflow and
requires inspection; it may already have changed the running container or database.

## GitHub configuration

Create environments named `publish` and `deploy` under **Settings → Environments**.
Configure the following values under **Settings → Secrets and variables → Actions**
or in the relevant environment. Both jobs need access to the same Docker Hub
repository settings; repository-level values are convenient for sharing them.

| Name | Kind | Used by | Value |
| --- | --- | --- | --- |
| `DOCKERHUB_USERNAME` | Secret | Publish, Deploy | Docker Hub account that owns the image repository |
| `DOCKERHUB_TOKEN` | Secret | Publish, Deploy | Docker Hub access token; publishing needs write access and deployment needs read access |
| `DOCKERHUB_REPOSITORY` | Variable, optional | Publish, Deploy | Image repository name; defaults to `registry` |
| `VPS_HOST` | Secret | Deploy | DNS hostname or IPv4 address, without a scheme or SSH options |
| `VPS_USER` | Secret | Deploy | SSH account with Docker and deployment-directory access |
| `VPS_PORT` | Secret, optional | Deploy | SSH port, 1–65535; defaults to 22 |
| `DEPLOY_SSH_KEY` | Secret | Deploy | Private SSH key authorized for `VPS_USER`; usable without an interactive passphrase |
| `VPS_KNOWN_HOSTS` | Secret, optional | Deploy | Trusted OpenSSH known_hosts entries; if omitted, the workflow discovers host keys with ssh-keyscan |
| `DEPLOY_PATH` | Secret | Deploy | Dedicated absolute directory, for example `/opt/registry`; only letters, digits, `_`, `-`, `.`, and `/`, with no `.` or `..` path components |
| `DEPLOY_ENV_FILE` | Secret | Deploy | Multiline Compose runtime settings; see below |

If credentials are stored separately in each environment, the deploy token may be
read-only. Ensure usernames and repository variables resolve to the same image name
in both environments. Prefer a repository-level `DOCKERHUB_REPOSITORY` variable and
avoid conflicting environment overrides. For `docker pull av3rgun/registry:...`,
set `DOCKERHUB_USERNAME` to `av3rgun` and `DOCKERHUB_REPOSITORY` to `registry`.
The Docker Hub repository name is independent of the GitHub repository name.
Use a lowercase Docker Hub repository name.

Example `DEPLOY_ENV_FILE`:

```dotenv
AUTH_SERVER=auth.xho.st
CERTBOT_EMAIL=operator@example.com
CERTBOT_STAGING=0
APP_PORT=8090
```

Set `AUTH_SERVER` to the issuer host used by your auth service. Use staging `1`
only when intentionally requesting test certificates. Do not include `REGISTRY_IMAGE`;
the workflow supplies it. This file configures Compose interpolation. The production
Compose file explicitly passes `APP_PORT`, `AUTH_SERVER`, `CERTBOT_EMAIL`, and `CERTBOT_STAGING`
to the container; adding arbitrary keys here does not automatically pass them through.
To expose more application settings, extend the Compose `environment` mapping.
The deployment publishes one API port: `APP_PORT` sets both the internal API
listener and the host port, defaulting to 8090 when unset or empty. For example,
`APP_PORT=8485` publishes `8485:8485` and makes the API available at
`http://VPS_IP:8485`. Select an available API port. The supplied Compose files do
not publish nginx HTTP/HTTPS listeners.
Update the `DEPLOY_ENV_FILE` secret for persistent changes because deployment
replaces the VPS `.env`. Shell environment variables can override Compose `.env`
values, so remove conflicting exported port values from the VPS account's environment.

Restrict the `deploy` environment's allowed deployment branch to `deploy`.
Configure `master` branch protection to require the Checks backend and frontend jobs.
Protect workflow changes through your normal PR review policy. Restrict who can push
to `deploy`. Optional required reviewers on the deploy environment add an approval
step if your operating policy requires one.

## VPS preparation

The VPS must have:

- Linux on an amd64 host, Bash, an SSH server, Docker Engine, and the Docker Compose
  plugin supporting `up --wait` and `--wait-timeout`.
- Outbound access to Docker Hub and inbound SSH access from GitHub-hosted runners.
- A dedicated SSH account that can run Docker without interactive sudo and write to
  `DEPLOY_PATH`. Docker access grants substantial host privileges; use a trusted account.
- One free TCP port matching `APP_PORT` (8090 by default), with network access
  configured for your API clients or existing reverse proxy.

Generate a dedicated SSH key on your administrator machine and add its public key
to the VPS account's `~/.ssh/authorized_keys`. Store the private key in `DEPLOY_SSH_KEY`.
Verify that noninteractive SSH login works before deploying.

If the deployment account cannot create directories under `/opt`, provision its
dedicated directory as an administrator first (replace the user and group):

```sh
sudo install -d -m 700 -o deployer -g deployer /opt/registry
```

No known_hosts secret is required for automatic host-key scanning. To pin the VPS
identity instead, collect the host key from a trusted administrator machine:

```sh
ssh-keyscan -p 22 -H vps.example.com > registry-known-hosts
ssh-keygen -lf registry-known-hosts
```

Verify the fingerprint against the VPS provider console or another trusted channel
before saving the file contents as `VPS_KNOWN_HOSTS`. Scanning alone does not establish
trust. For a nondefault SSH port, scan that port; known_hosts entries use
`[hostname]:port`. Use the same hostname in the scan and `VPS_HOST`.
When the server key changes, verify the replacement and update the secret.

The workflow uploads the versioned production Compose file on every deployment.
Set the API port using `APP_PORT` in `DEPLOY_ENV_FILE`, rather than editing only
the VPS `.env`. An existing reverse proxy can forward API requests to
`127.0.0.1:APP_PORT`. The supplied deployment exposes only the API; publishing
managed nginx traffic and configuring public HTTP-01 challenge routing are separate
infrastructure tasks.

Production uses the fixed Compose project name `registry`. Its volumes therefore
have names such as `registry_registry_data` and `registry_registry_certificates`.
Redeployments reuse them. An existing installation started under another project
name has different volumes: plan a backup and data migration before switching.
Do not run `docker compose down -v` on production unless you intend to delete its data.
Schedule certificate renewal separately as described in the main README.

## First deployment and promotion

Run these commands from the repository root, after committing and pushing the
workflow and Compose files to `master`. A repository without commits must receive
its initial commit first. If that was a direct push, manually run Publish Docker
Image on `master` and wait for success.

For the first deployment, use a commit with successful checks and a published image:

```sh
git fetch origin
git branch deploy <published-full-commit-sha>
git push -u origin deploy
```

The pushed commit must contain `deploy.yml` and `docker-compose.yml` for the flow to
work. Pushing the new branch triggers Deploy. Creating it does not build an image.

For later promotions, keep the deployment branch free of independent commits:

```sh
git fetch origin
git switch deploy
git merge --ff-only origin/deploy
git merge --ff-only <published-full-commit-sha>
git push origin deploy
```

Choose the published SHA explicitly if a newer `master` commit is still building.
Do not merge a PR into `deploy` with a new merge commit: that creates a SHA that was
not published from `master`. Fast-forward promotion preserves image identity.
If publishing fails, fix or rerun publishing before advancing `deploy`.

## VPS disk usage

Inspect storage on the VPS:

```sh
sudo docker system df -v
sudo du -xh --max-depth=1 /var/lib/docker /var/lib/containerd
```

The `-x` option skips mounted filesystems, including container root filesystems
under `/var/lib/docker/rootfs`, which would otherwise count image data again.
containerd image storage retains both compressed and extracted image layers, so
directory totals alone do not identify reclaimable space. Use Docker's storage
report before choosing cleanup operations.

For an immediate host-wide removal of images not referenced by any container:

```sh
sudo docker image prune -a -f
```

This can also remove locally cached rollback images and unused images of other
projects; they must be pulled again when needed. It leaves images referenced by
running or stopped containers and does not remove volumes. For unused build cache:

```sh
sudo docker builder prune -a -f
sudo docker system df
```

The VPS deployment pulls prebuilt images, so its build cache may already be empty.
Avoid deleting files directly under Docker/containerd storage or pruning production
volumes; databases, certificates, and managed configurations live in named volumes.
The deployment cleanup preserves two Registry versions but does not manage other
projects' images or application data. Its source-label filter does not cover legacy
images without the matching OCI label. Apply the updated Compose by redeploying
to activate log rotation; build and deploy a new image to receive the smaller build.

## Troubleshooting and rollback

| Symptom | Action |
| --- | --- |
| Deploy job skipped | Dispatch it on the `deploy` branch |
| Publishing fails with failed to reserve cache | Update to the workflow that skips cache export for pull_request_target and makes manual cache exports optional; rerunning an older workflow retains its old cache settings |
| Missing deployment setting | Add the named secret to the deploy environment or repository |
| Commit is not part of master | Promote an existing master commit without creating a deploy-only commit |
| Image unavailable | Compare the full image path in the error with Docker Hub's pull command; correct DOCKERHUB_REPOSITORY and environment overrides, verify credentials and publication of the full SHA tag, then rerun Deploy |
| SSH host-key verification fails | Verify the host fingerprint, hostname, and port, then correct `VPS_KNOWN_HOSTS` |
| Docker permission or Compose errors | Verify noninteractive Docker access for `VPS_USER` and a compatible Compose plugin |
| Health wait times out | Inspect container logs, migrations, nginx validation, and database access |
| Failed to bind host port / address already in use | Set APP_PORT to a free port in DEPLOY_ENV_FILE, and deploy a commit containing the single-port Compose file |

Inspect the VPS from the deployment directory:

```sh
cd /opt/registry
docker compose ps
docker compose logs --tail=200 registry
curl --fail http://127.0.0.1:8090/health
```

Replace 8090 with your configured `APP_PORT` if changed.

For an immediate image rollback, `.env.previous` stores the configuration before
the most recent startup attempt. It is one previous version, not a backup history.
After checking database compatibility and taking a backup, restore it on the VPS:

```sh
cd /opt/registry
test -f .env.previous
cp -p .env .env.failed
cp -p .env.previous .env
docker compose config --quiet
docker compose pull
docker compose up -d --remove-orphans --wait --wait-timeout 120
```

This reuses the previous digest and existing data volumes. It does not downgrade
the database or restore an older Compose file; startup migrations may already have
changed the schema. Restore a
database backup only through a deliberate recovery procedure appropriate to that version.
The deploy branch still points to the failed version, so another deployment would
reapply it. For a durable correction, prefer a reviewed fix or revert in `master`,
publish the resulting commit, and fast-forward `deploy` to it.

## References

- [GitHub workflow events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)
- [GitHub concurrency behavior](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)
- [Docker metadata-action](https://github.com/docker/metadata-action)
- [Docker Compose up and health waiting](https://docs.docker.com/reference/cli/docker/compose/up/)
