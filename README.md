# quick-deploy (`qd`)

Deploy any dockerised project from your laptop to a spare machine at home and get a public HTTPS URL:

```console
$ qd deploy ~/code/blog --name blog
https://blog.isalive.win

$ qd deploy            # no name: three random words
https://brave-golden-otter.isalive.win
```

## How it works

```
browser ──https──▶ Cloudflare edge ──tunnel──▶ cloudflared ──▶ traefik ──Host header──▶ app containers
                   *.isalive.win (wildcard)     (on the server, docker network "qd")
```

- **One** Cloudflare Tunnel with a **wildcard** DNS record `*.isalive.win`, created once by `qd setup`.
  No inbound ports, port forwarding or static IP needed, and Cloudflare handles TLS.
- **Traefik** on the server routes each hostname to the right container using Docker labels.
- **`qd deploy`** only uses SSH. It rsyncs the project to `~/.qd/apps/<name>/src` on the server, adds an
  override file with the Traefik labels, and runs `docker compose up -d --build` there.
  Deploys never call Cloudflare, so a new site is live as soon as its container answers.
  Images are built on the server, so arm64 (Mac) vs x86_64 (server) doesn't matter.

## Install

```sh
uv tool install --editable .     # puts `qd` on PATH (or: pipx install -e .)
```

No runtime dependencies (Python ≥ 3.10 standard library, plus `ssh` and `rsync`).

## One-time setup

**On the server (Fedora):**

```sh
sudo dnf install -y moby-engine docker-compose rsync    # or Docker CE from docker.com
sudo systemctl enable --now docker sshd
sudo usermod -aG docker $USER                           # log out and back in
# keep it running with the lid closed:
sudo sed -i 's/^#\?HandleLidSwitch=.*/HandleLidSwitch=ignore/' /etc/systemd/logind.conf && sudo systemctl restart systemd-logind
```

**On the Mac:**

```sh
ssh-copy-id me@fedora.local            # qd uses non-interactive ssh (BatchMode)

# Cloudflare API token (dash.cloudflare.com/profile/api-tokens) with:
#   Account > Cloudflare Tunnel > Edit,  Zone > DNS > Edit,  Zone > Zone > Read
export CLOUDFLARE_API_TOKEN=...
qd setup --host me@fedora.local --domain isalive.win
qd doctor
```

`setup` is idempotent. It:
1. checks docker, compose ≥ 2.24 and rsync on the server
2. creates (or reuses) a remotely-managed tunnel `quick-deploy` with ingress `*.isalive.win → http://qd-traefik:80`
3. creates a proxied `*.isalive.win` CNAME to the tunnel (refuses to overwrite an existing one without `--force`)
4. starts `qd-traefik` and `qd-cloudflared` on the server (`~/.qd/system`) with `restart: unless-stopped`

The API token is only used during setup and isn't saved. The tunnel token is stored on the server only,
in `~/.qd/system/.env` (mode 600). If you'd rather create the tunnel in the dashboard, pass `--tunnel-token`.

## Usage

| Command | |
|---|---|
| `qd deploy [DIR] [-n NAME] [-p PORT] [-s SERVICE] [-f FILE]` | Deploy DIR (default `.`). Prints the URL on stdout. |
| `qd ls` | List deployments and container status. |
| `qd logs NAME [-f] [--tail N] [-s SERVICE]` | Container logs. |
| `qd rm NAME [--volumes]` | Stop and delete a deployment (and its built images). |
| `qd doctor` | Check ssh → docker → traefik → tunnel → DNS → end-to-end. |
| `qd name` | Print a random three-word name. |

**Project detection:** `compose.yaml` / `docker-compose.yml` (preferred) or `Dockerfile` / `Containerfile`.

- **Compose:** the public service is `--service` if given, otherwise the only service that isn't a
  datastore (postgres, redis, ...), otherwise one named `web`/`app`/`frontend`/..., otherwise the only one
  with published ports. The port is `--port`, otherwise the service's first `ports:` target, otherwise its
  first `expose:`. Host port mappings are removed so apps can't collide (`--keep-ports` keeps them),
  and services without a restart policy get `unless-stopped` so they come back after a reboot.
- **Dockerfile:** the port comes from `EXPOSE` in the final stage, or `--port`. `.env` is passed to the container if present.

**Names:** without `--name`, redeploying the same directory reuses its URL. A new directory gets a random
`adjective-adjective-noun` name (`--new` forces a fresh one). Deploying to a name owned by another
directory fails unless you pass `--force`.

**Excluded from sync:** `.git`, `node_modules`, `.venv`, `__pycache__`, `.DS_Store`, plus any rsync
patterns listed in a `.qdignore` file. `.env` **is** synced.

## For agents

- Add `--json` anywhere (or set `QD_JSON=1`). stdout then has exactly one JSON object, and progress goes to stderr.
- Never prompts. ssh runs with `BatchMode=yes`.
- Exit codes: `0` ok · `1` error · `2` bad usage/config · `3` deployed but URL not answering within `--wait` · `4` no such deployment.
- Errors: `{"ok": false, "error": "...", "exit_code": N}`.

```console
$ qd deploy ./site --json
{
  "name": "quiet-amber-falcon",
  "url": "https://quiet-amber-falcon.isalive.win",
  "source": "/Users/me/site",
  "source_host": "macbook",
  "mode": "compose",
  "service": "web",
  "port": 3000,
  "deployed_at": "2026-10-05T07:30:00+00:00",
  "ok": true,
  "redeploy": false,
  "reachable": true,
  "http_status": 200
}
```

## Caveats

- Only one subdomain level (`x.isalive.win`, not `a.b.isalive.win`), because Cloudflare's free certificate covers only `*.isalive.win`.
- A specific DNS record (e.g. `www`) takes precedence over the wildcard.
- Sites are public. Put Cloudflare Access in front of the hostnames you want to keep private.
- On Fedora, bind mounts in your compose file may need SELinux `:z` labels.
