# quick-deploy (`qd`)

Deploy any dockerised project from your laptop to a spare machine at home and get a public HTTPS URL:

```console
$ qd deploy ~/code/blog --name blog
https://blog.example.com

$ qd deploy            # no name: three random words
https://brave-golden-otter.example.com
```

## How it works

```
browser ──https──▶ Cloudflare edge ──tunnel──▶ cloudflared ──▶ traefik ──Host header──▶ app containers
                   *.example.com (wildcard)     (on the server, docker network "qd")
```

- **One** Cloudflare Tunnel with a **wildcard** DNS record `*.example.com`, created once by `qd setup`.
  No inbound ports, port forwarding or static IP needed, and Cloudflare handles TLS.
- **Traefik** on the server routes each hostname to the right container using Docker labels.
- **`qd deploy`** only uses SSH. It rsyncs the project to `~/.qd/apps/<name>/src` on the server, adds an
  override file with the Traefik labels, and runs `docker compose up -d --build` there.
  Deploys never call Cloudflare, so a new site is live as soon as its container answers.
  Images are built on the server, so your machine's CPU architecture doesn't need to match the server's.

## Install

```sh
uv tool install --editable .     # puts `qd` on PATH (or: pipx install -e .)
```

No runtime dependencies (Python ≥ 3.10 standard library, plus `ssh` and `rsync`).

## One-time setup

You need:
- a **server**: any always-on Linux box you can ssh into (old laptop, mini PC, Raspberry Pi, VM).
  x86_64 and arm64 both work. Any distro works if it can run Docker Engine with compose ≥ 2.24.
- a **domain on Cloudflare** (free plan is fine).
- a **client** with Python ≥ 3.10, `ssh` and `rsync` (macOS or Linux).

**On the server:** install Docker, compose and rsync, and let your user run `docker` without sudo.
`qd setup` and `qd doctor` detect the distro and print the right commands if something is missing.

| Distro | Commands |
|---|---|
| Debian, Ubuntu, Raspberry Pi OS | `curl -fsSL https://get.docker.com \| sudo sh && sudo apt-get update && sudo apt-get install -y rsync` |
| Fedora | `sudo dnf install -y moby-engine docker-compose rsync` |
| RHEL, Rocky, Alma | `sudo dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo && sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin rsync` (needs `dnf-plugins-core`) |
| Arch | `sudo pacman -S --needed docker docker-compose rsync` |
| openSUSE | `sudo zypper install -y docker docker-compose rsync` |
| Alpine | `sudo apk add docker docker-cli-compose rsync && sudo rc-update add docker` |

```sh
sudo systemctl enable --now docker sshd    # Alpine: sudo service docker start
sudo usermod -aG docker $USER              # Alpine: sudo addgroup $USER docker. Then log out and back in.
```

[Rootless Docker](https://docs.docker.com/engine/security/rootless/) also works: `qd setup` finds its
socket through the active docker context. Podman is not supported.

If the server is a laptop, stop it from sleeping when the lid is closed:

```sh
sudo sed -i 's/^#\?HandleLidSwitch=.*/HandleLidSwitch=ignore/' /etc/systemd/logind.conf && sudo systemctl restart systemd-logind
```

**On your machine:**

```sh
ssh-copy-id me@homeserver.local        # qd uses non-interactive ssh (BatchMode)

# Cloudflare API token (dash.cloudflare.com/profile/api-tokens) with:
#   Account > Cloudflare Tunnel > Edit,  Zone > DNS > Edit,  Zone > Zone > Read
export CLOUDFLARE_API_TOKEN=...
qd setup --host me@homeserver.local --domain example.com
qd doctor
```

`--host` is anything `ssh` accepts, including a `Host` alias from `~/.ssh/config` (handy for jump hosts or
non-standard ports). `--ssh-port`, `--ssh-key` and `--tunnel-name` are also available. Settings are saved
to `~/.config/qd/config.json` (`QD_CONFIG` overrides the path), so rerun `qd setup` with new flags to change them.

`setup` is idempotent. It:
1. checks docker, compose ≥ 2.24 and rsync on the server (no bash needed: remote scripts are plain `sh`)
2. creates (or reuses) a remotely-managed tunnel `quick-deploy` with ingress `*.example.com → http://qd-traefik:80`
3. creates a proxied `*.example.com` CNAME to the tunnel (refuses to overwrite an existing one without `--force`)
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
  "url": "https://quiet-amber-falcon.example.com",
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

- Only one subdomain level (`x.example.com`, not `a.b.example.com`), because Cloudflare's free certificate covers only `*.example.com`.
- A specific DNS record (e.g. `www`) takes precedence over the wildcard.
- Sites are public. Put Cloudflare Access in front of the hostnames you want to keep private.
- On SELinux distros (Fedora, RHEL), bind mounts in your compose file may need `:z` labels.
