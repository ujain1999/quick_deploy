"""qd: deploy dockerised projects to your home server at https://<name>.<domain>.

Progress always goes to stderr. stdout carries only the result: plain text by default,
or a single JSON object with --json (accepted anywhere on the command line).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from quick_deploy import __version__
from quick_deploy.cloudflare import TRAEFIK_SERVICE, Cloudflare
from quick_deploy.config import Config
from quick_deploy.errors import ERROR, NOT_FOUND, OK, UNREACHABLE, USAGE, QDError
from quick_deploy.names import random_name
from quick_deploy.project import (
    build_expose,
    compose_override,
    detect_project,
    dockerfile_compose,
    pick_service,
    remote_to_local,
    tunnel_services,
    valid_name,
)
from quick_deploy.remote import Remote, heredoc, shq
from quick_deploy.server import (
    MIN_COMPOSE,
    TRAEFIK_404,
    bootstrap,
    docker_hint,
    list_apps,
    pkg_hint,
    probe,
    server_status,
    version_at_least,
    wait_until_up,
)

EPILOG = """\
exit codes:
  0 ok, 1 error, 2 bad usage, 3 deployed but URL not reachable yet, 4 deployment not found

typical flow:
  qd setup --host me@homeserver.local --domain example.com   # once (needs CLOUDFLARE_API_TOKEN)
  qd deploy                                                # deploy ./ at a random name, prints URL
  qd deploy ~/code/blog --name blog                        # https://blog.example.com
  qd ls --json
"""

JSON = False


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def emit(result: dict, human: str) -> None:
    if JSON:
        print(json.dumps(result, indent=2))
    elif human:
        print(human)


# --- setup --------------------------------------------------------------------


def cmd_setup(a: argparse.Namespace) -> int:
    cfg = Config.load()
    for field in ("host", "domain", "ssh_port", "ssh_key", "tunnel_name"):
        if (v := getattr(a, field)) is not None:
            setattr(cfg, field, v)
    cfg.require()
    cfg.save()
    r = Remote(cfg)

    log(f"checking {cfg.host} ...")
    st = server_status(r)
    os_ids = st.get("os", "")
    if not st.get("docker"):
        raise QDError(f"docker is not usable by this user on the server. Try:\n  {docker_hint(os_ids)}")
    if not version_at_least(st.get("compose", ""), MIN_COMPOSE):
        raise QDError(f"docker compose >= {'.'.join(map(str, MIN_COMPOSE))} required on the server (found {st.get('compose') or 'none'})")
    if not st.get("rsync"):
        raise QDError(f"rsync is missing on the server: {pkg_hint(os_ids, 'rsync')}")

    token = a.tunnel_token
    if token:
        log(
            f"using the given tunnel token. In the Cloudflare dashboard, make sure the tunnel has a public "
            f"hostname *.{cfg.domain} -> {TRAEFIK_SERVICE} and DNS has *.{cfg.domain} CNAME <tunnel-id>.cfargotunnel.com"
        )
    else:
        cf_token = a.cf_token or os.environ.get("CLOUDFLARE_API_TOKEN")
        if not cf_token:
            raise QDError(
                "need a Cloudflare API token (CLOUDFLARE_API_TOKEN or --cf-token) with permissions "
                "Account>Cloudflare Tunnel>Edit, Zone>DNS>Edit, Zone>Zone>Read; "
                "or pass --tunnel-token for a tunnel you configured yourself",
                USAGE,
            )
        cf = Cloudflare(cf_token)
        log(f"provisioning Cloudflare tunnel {cfg.tunnel_name!r} and DNS *.{cfg.domain} ...")
        zone_id, account_id = cf.zone(cfg.domain)
        tunnel_id = cf.ensure_tunnel(account_id, cfg.tunnel_name)
        cf.put_ingress(account_id, tunnel_id, cfg.domain)
        cf.ensure_wildcard_dns(zone_id, cfg.domain, tunnel_id, a.force)
        token = cf.tunnel_token(account_id, tunnel_id)
        cfg.tunnel_id = tunnel_id
        cfg.save()

    log("starting traefik + cloudflared on the server ...")
    bootstrap(r, token)

    log("checking the tunnel end to end ...")
    ok, status = wait_until_tunnel(cfg.domain, timeout=60)
    emit(
        {"ok": True, "host": cfg.host, "domain": cfg.domain, "tunnel_id": cfg.tunnel_id, "tunnel_reachable": ok},
        f"ready: deploy with `qd deploy` -> https://<name>.{cfg.domain}"
        + ("" if ok else f"\nwarning: tunnel not answering yet (last status {status}); run `qd doctor` in a minute"),
    )
    return OK


def wait_until_tunnel(domain: str, timeout: float) -> tuple[bool, int | None]:
    """A random hostname should reach Traefik and get its no-route 404."""
    url = f"https://qd-probe-{random_name().replace('-', '')[:20]}.{domain}/"
    deadline = time.monotonic() + timeout
    while True:
        status, body = probe(url)
        if status == 404 and body.strip() == TRAEFIK_404:
            return True, status
        if time.monotonic() >= deadline:
            return False, status
        time.sleep(3)


# --- deploy -------------------------------------------------------------------


def cmd_deploy(a: argparse.Namespace) -> int:
    cfg = Config.load().require()
    src = Path(a.dir).expanduser().resolve()
    if not src.is_dir():
        raise QDError(f"{src} is not a directory", USAGE)
    proj = detect_project(src, a.file)
    r = Remote(cfg)
    apps = list_apps(r)
    host = socket.gethostname()

    name = (a.name or "").lower()
    if name:
        if not valid_name(name):
            raise QDError(f"invalid name {name!r}: use lowercase letters, digits and hyphens (one DNS label)", USAGE)
        prev = apps.get(name)
        if prev and (prev.source, prev.source_host) != (str(src), host) and not a.force:
            raise QDError(
                f"{name}.{cfg.domain} is already used by {prev.source_host}:{prev.source}; "
                "pick another --name or pass --force to replace it",
                USAGE,
            )
    elif not a.new:
        # Redeploying the same directory keeps its URL.
        name = next((n for n, p in apps.items() if (p.source, p.source_host) == (str(src), host)), "")
    redeploy = name in apps
    name = name or random_name(set(apps))
    url = f"https://{name}.{cfg.domain}"
    log(f"{'redeploying' if redeploy else 'deploying'} {src} -> {url} ({proj.mode}: {proj.file})")

    warnings: list[str] = []
    r.run(f'mkdir -p "$QD/apps/{name}/src"')
    log("syncing files ...")
    r.sync(src, f".qd/apps/{name}/src")

    if proj.mode == "compose":
        raw = r.output(f'cd "$QD/apps/{name}/src" && docker compose -f {shq(proj.file)} config --format json')
        services = json.loads(raw).get("services") or {}

        def to_local(path: str) -> Path | None:
            # src/ on the server mirrors src locally, so Dockerfiles can be read here.
            return remote_to_local(path, f"/.qd/apps/{name}/src", src)

        service, port = pick_service(services, a.service, a.port, lambda spec: build_expose(spec, to_local))
        for t in tunnel_services(services):
            warnings.append(
                f"service {t!r} runs its own Cloudflare tunnel; qd already routes {url}, so you may not need it"
            )
        generated = "qd.override.yml"
        content = compose_override(services, service, port, name, cfg.domain, a.keep_ports)
        files = [f"src/{proj.file}", generated]
    else:
        port = a.port or proj.exposed_port
        if not port:
            raise QDError(f"no EXPOSE in {proj.file}; pass --port", USAGE)
        service, generated = "app", "qd.compose.yml"
        content = dockerfile_compose(name, cfg.domain, proj.file, port, proj.has_env)
        files = [generated]

    meta = {
        "name": name,
        "url": url,
        "source": str(src),
        "source_host": host,
        "mode": proj.mode,
        "service": service,
        "port": port,
        "deployed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    for w in warnings:
        log(f"warning: {w}")
    log(f"building and starting on the server (service {service!r}, port {port}) ...")
    fargs = " ".join(f"-f {shq(f)}" for f in files)
    r.run(
        f'cd "$QD/apps/{name}"\n'
        + heredoc(generated, content)
        + heredoc("meta.json", json.dumps(meta))
        + f"docker compose -p qd-{name} {fargs} up -d --build --remove-orphans\n",
    )

    reachable, status = None, None
    if a.wait > 0:
        log(f"waiting up to {a.wait}s for {url} ...")
        reachable, status = wait_until_up(url, a.wait)
    result = {**meta, "ok": True, "redeploy": redeploy, "reachable": reachable, "http_status": status, "warnings": warnings}
    emit(result, url)
    if reachable is False:
        log(f"warning: deployed, but {url} isn't answering yet (last status {status}). Check: qd logs {name}")
        return UNREACHABLE
    return OK


# --- ls / logs / rm -----------------------------------------------------------


def cmd_ls(a: argparse.Namespace) -> int:
    cfg = Config.load().require()
    apps = sorted(list_apps(Remote(cfg)).values(), key=lambda x: x.deployed_at or "", reverse=True)
    rows = [("NAME", "STATUS", "URL", "SOURCE", "DEPLOYED")] + [
        (x.name, x.status, x.url, f"{x.source_host}:{x.source}", (x.deployed_at or "")[:16]) for x in apps
    ]
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    table = "\n".join("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in rows)
    emit({"ok": True, "apps": [x.to_dict() for x in apps]}, table if apps else "no deployments")
    return OK


def _require_name(name: str) -> str:
    name = name.lower()
    if not valid_name(name):
        raise QDError(f"invalid name {name!r}", USAGE)
    return name


def _exists_check(name: str) -> str:
    return f'[ -d "$QD/apps/{name}" ] || {{ echo "no deployment named {name}" >&2; exit {NOT_FOUND}; }}\n'


def cmd_logs(a: argparse.Namespace) -> int:
    cfg = Config.load().require()
    name = _require_name(a.name)
    cmd = f"docker compose -p qd-{name} logs --no-color --tail {int(a.tail)}"
    if a.follow:
        cmd += " -f"
    if a.timestamps:
        cmd += " -t"
    if a.service:
        cmd += f" {shq(a.service)}"
    Remote(cfg).run(_exists_check(name) + cmd + "\n", stdout=sys.stdout)
    return OK


def cmd_rm(a: argparse.Namespace) -> int:
    cfg = Config.load().require()
    name = _require_name(a.name)
    down = f"docker compose -p qd-{name} down --remove-orphans --rmi local" + (" --volumes" if a.volumes else "")
    log(f"removing {name} ...")
    Remote(cfg).run(
        _exists_check(name)
        + down
        + "\n"
        # Containers may have left root-owned files in bind mounts; delete those via docker.
        + f'rm -rf "$QD/apps/{name}" 2>/dev/null || '
        + f'docker run --rm --security-opt label=disable -v "$QD/apps:/apps" alpine rm -rf "/apps/{name}"\n'
    )
    emit({"ok": True, "name": name, "removed": True}, f"removed {name}")
    return OK


# --- doctor -------------------------------------------------------------------


def cmd_doctor(a: argparse.Namespace) -> int:
    cfg = Config.load().require()
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "", hint: str = "") -> None:
        checks.append({"check": name, "ok": ok, "detail": detail, **({"hint": hint} if hint and not ok else {})})

    try:
        st = server_status(Remote(cfg))
        check("ssh", True, f"{cfg.host} ({st.get('os') or 'unknown os'})")
    except QDError as e:
        st = None
        check("ssh", False, str(e), f"make `ssh {cfg.host}` work with a key (ssh-copy-id {cfg.host})")
    if st is not None:
        os_ids = st.get("os", "")
        check("docker", bool(st.get("docker")), st.get("docker", ""), docker_hint(os_ids))
        check(
            "compose",
            version_at_least(st.get("compose", ""), MIN_COMPOSE),
            st.get("compose", ""),
            f"need docker compose >= {'.'.join(map(str, MIN_COMPOSE))}; update Docker and its compose plugin",
        )
        check("rsync", bool(st.get("rsync")), st.get("rsync", ""), pkg_hint(os_ids, "rsync"))
        check("network", st.get("network") == "ok", "qd", "run qd setup")
        check("traefik", st.get("traefik") == "running", st.get("traefik") or "absent", "run qd setup")
        check("cloudflared", st.get("cloudflared") == "running", st.get("cloudflared") or "absent", "run qd setup")
        conns = st.get("tunnel_connections", "0")
        check("tunnel", conns not in ("", "0"), f"{conns} connections registered (24h)", "docker logs qd-cloudflared")
    try:
        addr = socket.getaddrinfo(f"qd-probe.{cfg.domain}", 443)[0][4][0]
        check("dns", True, f"*.{cfg.domain} -> {addr}")
    except OSError as e:
        check("dns", False, str(e), f"add a proxied CNAME *.{cfg.domain} -> <tunnel-id>.cfargotunnel.com")
    ok_e2e, status = wait_until_tunnel(cfg.domain, timeout=0)
    check("end-to-end", ok_e2e, f"https://<random>.{cfg.domain} -> HTTP {status}", "expected Traefik's 404 for an unknown name")

    healthy = all(c["ok"] for c in checks)
    lines = [
        f"{'ok  ' if c['ok'] else 'FAIL'}  {c['check']:<12} {c['detail']}" + (f"\n      -> {c['hint']}" if c.get("hint") else "")
        for c in checks
    ]
    emit({"ok": healthy, "checks": checks}, "\n".join(lines))
    return OK if healthy else ERROR


# --- misc -----------------------------------------------------------------------


def cmd_name(a: argparse.Namespace) -> int:
    n = random_name()
    emit({"ok": True, "name": n}, n)
    return OK


def cmd_version(a: argparse.Namespace) -> int:
    emit({"ok": True, "version": __version__}, __version__)
    return OK


# --- argument parsing -----------------------------------------------------------


class Parser(argparse.ArgumentParser):
    def error(self, message: str):  # report usage errors like every other error
        raise QDError(f"{message} (see qd {self.prog.split()[-1]} -h)", USAGE)


def build_parser() -> Parser:
    p = Parser(
        prog="qd",
        description="Deploy dockerised projects to your home server at https://<name>.<domain>. "
        "Add --json anywhere for machine-readable output.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", metavar="COMMAND", parser_class=Parser)

    s = sub.add_parser("setup", help="one-time: prepare the server and the Cloudflare tunnel (idempotent)")
    s.add_argument("--host", help="ssh destination of the server, e.g. me@homeserver.local or an ~/.ssh/config alias")
    s.add_argument("--domain", help="domain on your Cloudflare account, e.g. example.com")
    s.add_argument("--ssh-port", type=int)
    s.add_argument("--ssh-key", help="ssh identity file")
    s.add_argument("--tunnel-name", help="Cloudflare tunnel name (default quick-deploy)")
    s.add_argument("--cf-token", help="Cloudflare API token (default: $CLOUDFLARE_API_TOKEN)")
    s.add_argument("--tunnel-token", help="use an existing tunnel's token instead of the Cloudflare API")
    s.add_argument("--force", action="store_true", help="overwrite an existing *.<domain> DNS record")
    s.set_defaults(func=cmd_setup)

    d = sub.add_parser(
        "deploy",
        help="build and run a project on the server; prints its URL",
        description="Sync DIR to the server and run it with docker compose (or its Dockerfile). "
        "Without --name, a redeploy of the same directory keeps its URL; a new one gets a random three-word name.",
    )
    d.add_argument("dir", nargs="?", default=".", help="project directory (default .)")
    d.add_argument("-n", "--name", help="subdomain: <name>.<domain>")
    d.add_argument("-p", "--port", type=int, help="container port to route to (default: detected)")
    d.add_argument("-s", "--service", help="compose service to expose (default: detected)")
    d.add_argument("-f", "--file", help="compose file or Dockerfile, relative to DIR (default: detected)")
    d.add_argument("--new", action="store_true", help="always use a fresh random name")
    d.add_argument("--force", action="store_true", help="take over a name used by another directory")
    d.add_argument("--keep-ports", action="store_true", help="keep host port mappings from the compose file")
    d.add_argument("--wait", type=int, default=90, metavar="SECONDS", help="wait for the URL to answer (0 = don't; default 90)")
    d.set_defaults(func=cmd_deploy)

    sub.add_parser("ls", aliases=["list"], help="list deployments").set_defaults(func=cmd_ls)

    lg = sub.add_parser("logs", help="show container logs (raw text, even with --json)")
    lg.add_argument("name")
    lg.add_argument("-f", "--follow", action="store_true")
    lg.add_argument("--tail", type=int, default=200)
    lg.add_argument("-t", "--timestamps", action="store_true")
    lg.add_argument("-s", "--service", help="only this compose service")
    lg.set_defaults(func=cmd_logs)

    rm = sub.add_parser("rm", help="stop and delete a deployment")
    rm.add_argument("name")
    rm.add_argument("--volumes", action="store_true", help="also delete its named volumes (data!)")
    rm.set_defaults(func=cmd_rm)

    sub.add_parser("doctor", help="check ssh, docker, tunnel and DNS end to end").set_defaults(func=cmd_doctor)
    sub.add_parser("name", help="print a random three-word name").set_defaults(func=cmd_name)
    sub.add_parser("version", help="print the version").set_defaults(func=cmd_version)
    return p


def main(argv: list[str] | None = None) -> None:
    global JSON
    argv = list(sys.argv[1:] if argv is None else argv)
    JSON = "--json" in argv or os.environ.get("QD_JSON") == "1"
    argv = [x for x in argv if x != "--json"]
    try:
        a = build_parser().parse_args(argv)
        if not getattr(a, "func", None):
            build_parser().print_help(sys.stderr)
            sys.exit(USAGE)
        code = a.func(a)
    except QDError as e:
        code = e.code
        if JSON:
            print(json.dumps({"ok": False, "error": str(e), "exit_code": code}, indent=2))
        else:
            log(f"error: {e}")
    except KeyboardInterrupt:
        code = ERROR
    sys.exit(code)
