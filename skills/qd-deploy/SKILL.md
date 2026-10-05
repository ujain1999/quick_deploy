---
name: qd-deploy
description: Deploy a dockerised project to the user's home server with quick-deploy (`qd`) and get a public HTTPS URL. Use when the user asks to deploy, ship, host, publish, or "put online" a project or app, to share a preview link, or to check on, view logs of, list, or remove a qd deployment.
---

# Deploying with qd

`qd` syncs a project directory to the user's server over ssh, runs it with `docker compose up -d --build`
there, and routes `https://<name>.<domain>` to it through a Cloudflare Tunnel. It never prompts, so it's safe
to run non-interactively.

**Always pass `--json`.** stdout then holds exactly one JSON object and progress goes to stderr. Parse
stdout; read stderr only to explain failures.

## 1. Preflight

```sh
command -v qd || echo "qd not installed"
qd doctor --json
```

- **`qd` not installed:** tell the user. It installs from the quick-deploy repo with
  `uv tool install --editable <repo>` (or `pipx install -e <repo>`).
- **Exit 2 with "not configured":** the one-time `qd setup` hasn't been run. Setup needs the user's server
  (`--host`), their Cloudflare domain (`--domain`) and a `CLOUDFLARE_API_TOKEN`, and it creates a tunnel and
  a wildcard DNS record on their account. Ask the user for these and confirm before running it. Never invent
  a host or domain.
- **Any check has `"ok": false`:** each failed check has a `hint` with the fix. Show the user the hints;
  most fixes (installing Docker, `ssh-copy-id`, adding the user to the `docker` group) need sudo on the
  server or the user's own credentials, so don't run them yourself unless asked.

Skip `doctor` if you've already deployed successfully in this session.

## 2. Make sure the project is deployable

qd looks in the project directory, in this order, for `compose.yaml`, `compose.yml`,
`docker-compose.yaml`, `docker-compose.yml`, then `Dockerfile` / `Containerfile`.

If there's neither, write a `Dockerfile` for the project first (tell the user you're adding one). Make sure:

- the server listens on **`0.0.0.0`**, not `127.0.0.1`/`localhost`. This is the most common cause of a
  deploy that builds fine but never answers (e.g. `vite --host 0.0.0.0`, `next start -H 0.0.0.0`,
  `uvicorn --host 0.0.0.0`, `flask run --host 0.0.0.0`).
- the final stage has an `EXPOSE <port>` matching the port the app listens on (otherwise pass `--port`).
- it runs a production server where practical, not a dev server with file watching.

For compose projects, qd picks the public service and port automatically (see "Choosing the service and port"
below). It removes host `ports:` mappings and adds `restart: unless-stopped`; you don't need to change the
compose file for that.

Before deploying, check what will be uploaded: the whole directory is rsynced except `.git`,
`node_modules`, `.venv`, `__pycache__`, `.DS_Store` and patterns in `.qdignore`. **`.env` is uploaded** and
passed to the container. If the project has large build outputs, datasets or secrets that shouldn't go to the
server, add them to a `.qdignore` (rsync exclude patterns, one per line).

## 3. Deploy

```sh
qd deploy <DIR> --json                 # redeploy keeps the URL; a new dir gets a random three-word name
qd deploy <DIR> --name blog --json     # https://blog.<domain>
```

Options:

| Flag | Use |
|---|---|
| `-n, --name NAME` | Subdomain. One DNS label: lowercase letters, digits, hyphens. Only if the user wants a specific name. |
| `-p, --port PORT` | Container port, when detection fails or picks the wrong one. |
| `-s, --service SVC` | Compose service to expose, when there are several candidates. |
| `-f, --file FILE` | Compose file or Dockerfile to use, relative to DIR. |
| `--wait SECONDS` | How long to wait for the URL to answer (default 90). Raise it for slow-starting apps. |
| `--new` | Use a fresh random name instead of reusing this directory's URL. |
| `--keep-ports` | Keep host port mappings from the compose file. Rarely wanted. |
| `--force` | Take over a name owned by another directory. **Ask the user first**: it replaces someone else's site. |

The build runs on the server, so it can take minutes. Use a long command timeout (10 minutes) and don't
interrupt it.

Successful output:

```json
{
  "name": "quiet-amber-falcon",
  "url": "https://quiet-amber-falcon.example.com",
  "mode": "compose",
  "service": "web",
  "port": 3000,
  "ok": true,
  "redeploy": false,
  "reachable": true,
  "http_status": 200,
  "warnings": []
}
```

Give the user the `url`. Mention any `warnings`. If `http_status` is 4xx/5xx even though `reachable` is
true, the app is up but erroring on `/`; look at the logs.

## 4. Handle the exit code

| Exit | Meaning | What to do |
|---|---|---|
| 0 | Deployed and answering | Report the URL. |
| 1 | Error (ssh, rsync, build, compose) | Read the `error` field and stderr. Build errors come from `docker compose`: fix the Dockerfile/compose file and redeploy. |
| 2 | Bad usage or config | The `error` says what to pass: usually `--service` (several candidate services), `--port` (no port found), or a name conflict. |
| 3 | Deployed, but URL didn't answer within `--wait` | Run `qd logs <name> --tail 100`. Usually the app crashed, listens on localhost, or uses a different port than qd routed to. Fix and redeploy. If it's just slow, check again or redeploy with a bigger `--wait`. |
| 4 | No such deployment | Check the name with `qd ls --json`. |

Errors look like `{"ok": false, "error": "...", "exit_code": N}`.

Redeploying after a fix is just running the same `qd deploy <DIR> --json` again: same directory, same URL.

## Choosing the service and port

For compose projects, without `--service` qd picks: the only service, else the only one that isn't a
datastore (postgres, mysql, redis, mongo, ...), else one named `web`, `app`, `frontend`, `site`, `server`,
`nginx` or `caddy`, else the only one with `ports:`. The port is `--port`, else the service's first
`ports:` target (the container side), else its first `expose:`, else `EXPOSE` in the Dockerfile it builds.

If the guess is wrong, the result's `service`/`port` will show it; redeploy with `-s`/`-p`.

## Other commands

```sh
qd ls --json                          # {"apps": [{"name", "url", "status", "source", "source_host", ...}]}
qd logs NAME --tail 200 [-s SERVICE]  # raw text even with --json; don't use -f (it never exits)
qd rm NAME --json                     # stop and delete a deployment and its images
qd rm NAME --volumes --json           # ...and its named volumes (deletes data)
```

`status` in `qd ls` is `running`, `degraded` (some containers down), `stopped` or `missing`.

**`qd rm` is destructive and takes the site offline. Only run it when the user asked to remove that
deployment, and never add `--volumes` without explicit confirmation.**

## Things to tell the user when relevant

- Deployed sites are **public** to anyone with the URL. If the project has no auth and exposes anything
  sensitive, say so; they can put Cloudflare Access in front of the hostname.
- Only one subdomain level works (`x.example.com`, not `a.b.example.com`).
- On SELinux servers (Fedora, RHEL), bind mounts in the compose file may need a `:z` suffix if the container
  gets permission errors.
