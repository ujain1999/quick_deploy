"""Project detection and generation of the compose files that wire an app into Traefik.

Routing works through Docker labels: every deployment's web service joins the shared
`qd` network and carries Traefik labels for `Host(<name>.<domain>)`. Cloudflare sends
all `*.<domain>` traffic through the tunnel to Traefik, so a deploy never needs to
touch Cloudflare.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from quick_deploy.errors import USAGE, QDError

NETWORK = "qd"
COMPOSE_NAMES = ["compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"]
DOCKERFILE_NAMES = ["Dockerfile", "Containerfile", "dockerfile"]
# Services preferred as the public one when a compose file has several candidates.
PREFERRED_SERVICES = ["web", "app", "frontend", "site", "server", "nginx", "caddy"]
# Images never picked as the public service.
DATASTORES = ("postgres", "mysql", "mariadb", "redis", "valkey", "mongo", "memcached", "rabbitmq", "elasticsearch", "minio")


def _is_datastore(spec: dict) -> bool:
    image = (spec.get("image") or "").rsplit("/", 1)[-1]
    return image.startswith(DATASTORES)

_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def valid_name(name: str) -> bool:
    """A single DNS label (Cloudflare's free certificate covers only one level of subdomain)."""
    return bool(_NAME_RE.match(name))


@dataclass
class Project:
    mode: str  # "compose" or "dockerfile"
    file: str  # path relative to the project dir
    exposed_port: int | None = None  # from Dockerfile EXPOSE


def detect_project(root: Path, file: str | None = None) -> Project:
    if file:
        if not (root / file).is_file():
            raise QDError(f"{root / file} does not exist", USAGE)
        candidates = [file]
    else:
        candidates = [n for n in COMPOSE_NAMES + DOCKERFILE_NAMES if (root / n).is_file()]
        if not candidates:
            raise QDError(f"no compose file or Dockerfile found in {root}", USAGE)
    chosen = candidates[0]
    if "compose" in Path(chosen).name.lower():
        return Project("compose", chosen)
    port = parse_expose((root / chosen).read_text(errors="replace"))
    return Project("dockerfile", chosen, exposed_port=port)


def _leading_int(s: str) -> int | None:
    m = re.match(r"\d+", str(s))
    return int(m.group()) if m else None


def parse_expose(dockerfile: str, target: str | None = None) -> int | None:
    """First numeric EXPOSE port of build stage `target` (default: the final stage)."""
    port, stage = None, None
    for line in dockerfile.splitlines():
        parts = line.split()
        if not parts:
            continue
        instr = parts[0].upper()
        if instr == "FROM":
            if target and stage == target.lower():
                return port
            port = None
            args = [p for p in parts[1:] if not p.startswith("--")]
            stage = args[2].lower() if len(args) >= 3 and args[1].upper() == "AS" else None
        elif instr == "EXPOSE" and port is None:
            port = next((p for p in map(_leading_int, parts[1:]) if p), None)
    return port


def remote_to_local(path: str, remote_root: str, local_root: Path) -> Path | None:
    """Map an absolute server path under remote_root (e.g. `/.qd/apps/x/src`) to local_root."""
    _, sep, rest = path.partition(remote_root)
    if not sep or (rest and not rest.startswith("/")):
        return None
    return local_root / rest.lstrip("/")


def build_expose(spec: dict, to_local: Callable[[str], Path | None]) -> int | None:
    """EXPOSE port of the Dockerfile a compose service builds, read from the local copy."""
    build = spec.get("build")
    if not isinstance(build, dict):
        return None
    target = build.get("target")
    if inline := build.get("dockerfile_inline"):
        return parse_expose(inline, target)
    dockerfile = build.get("dockerfile") or "Dockerfile"
    if dockerfile.startswith("/"):
        path = to_local(dockerfile)
    else:
        context = to_local(build.get("context") or "")
        path = context / dockerfile if context else None
    try:
        return parse_expose(path.read_text(errors="replace"), target) if path else None
    except OSError:
        return None


def tunnel_services(services: dict) -> list[str]:
    """Services that run their own cloudflared (redundant behind qd)."""
    def repo(image: str) -> str:
        return image.split("@")[0].rsplit("/", 1)[-1].split(":")[0]

    return sorted(n for n, spec in services.items() if repo(spec.get("image") or "") == "cloudflared")


def pick_service(
    services: dict,
    want: str | None,
    port: int | None,
    dockerfile_port: Callable[[dict], int | None] | None = None,
) -> tuple[str, int]:
    """Choose the public service and its container port from `docker compose config` JSON.

    Port order: explicit `port`, the service's `ports:` targets, its `expose:`, then
    `dockerfile_port(spec)` (EXPOSE in the Dockerfile it builds).
    """
    names = sorted(services)
    if not names:
        raise QDError("compose file defines no services")
    if want:
        if want not in services:
            raise QDError(f"service {want!r} not found; services: {', '.join(names)}", USAGE)
        svc = want
    elif len(names) == 1:
        svc = names[0]
    else:
        candidates = [n for n in names if not _is_datastore(services[n])]
        preferred = [n for n in PREFERRED_SERVICES if n in candidates]
        published = [n for n in candidates if services[n].get("ports")]
        if len(candidates) == 1:
            svc = candidates[0]
        elif preferred:
            svc = preferred[0]
        elif len(published) == 1:
            svc = published[0]
        else:
            raise QDError(f"several services ({', '.join(names)}); pass --service to pick the public one", USAGE)

    spec = services[svc]
    if spec.get("network_mode"):
        raise QDError(f"service {svc!r} uses network_mode={spec['network_mode']}; it can't join the routing network")
    if not port:
        for p in spec.get("ports") or []:
            if port := _leading_int(p.get("target", "")):
                break
    if not port:
        for e in spec.get("expose") or []:
            if port := _leading_int(e):
                break
    if not port and dockerfile_port:
        port = dockerfile_port(spec)
    if not port:
        raise QDError(
            f"can't tell which port service {svc!r} listens on (no ports:, expose: or Dockerfile EXPOSE); pass --port",
            USAGE,
        )
    return svc, port


def traefik_labels(name: str, domain: str, port: int) -> dict[str, str]:
    router = f"qd-{name}"
    return {
        "traefik.enable": "true",
        "traefik.docker.network": NETWORK,
        f"traefik.http.routers.{router}.rule": f"Host(`{name}.{domain}`)",
        f"traefik.http.routers.{router}.entrypoints": "web",
        f"traefik.http.services.{router}.loadbalancer.server.port": str(port),
        "dev.qd.app": name,
    }


def _q(s: str) -> str:
    # JSON strings are valid YAML double-quoted scalars.
    return json.dumps(s)


def _labels_yaml(labels: dict[str, str], indent: str) -> list[str]:
    return [f"{indent}labels:"] + [f"{indent}  {_q(k)}: {_q(v)}" for k, v in labels.items()]


_NETWORKS_FOOTER = f"networks:\n  {NETWORK}:\n    name: {NETWORK}\n    external: true\n"


def compose_override(
    services: dict, svc: str, port: int, name: str, domain: str, keep_ports: bool = False
) -> str:
    """Override file layered on the user's compose file with `-f`.

    - public service: Traefik labels, joins the `qd` network (keeping its own networks)
    - host port publishing removed (unless keep_ports) so apps can't collide on the server
    - restart: unless-stopped where no restart policy is set, so sites survive reboots
    """
    out = ["# generated by qd; do not edit", "services:"]
    for s in sorted(services):
        spec = services[s]
        lines: list[str] = []
        if s == svc:
            lines += _labels_yaml(traefik_labels(name, domain, port), "    ")
            # Networks merge by key, so restating the service's own networks keeps them.
            nets = [n for n in (spec.get("networks") or {}) if n != NETWORK] or ["default"]
            lines.append("    networks:")
            lines += [f"      {_q(n)}: {{}}" for n in [*nets, NETWORK]]
        if spec.get("ports") and not keep_ports:
            lines.append("    ports: !reset []")
        if not spec.get("restart"):
            lines.append("    restart: unless-stopped")
        if lines:
            out.append(f"  {_q(s)}:")
            out += lines
    return "\n".join(out) + "\n" + _NETWORKS_FOOTER


def dockerfile_compose(name: str, domain: str, dockerfile: str, port: int) -> str:
    """Complete compose file for a project that only has a Dockerfile (lives next to src/).

    `.env` is optional because it may be absent or excluded from the sync by `.qdignore`.
    """
    out = [
        "# generated by qd; do not edit",
        "services:",
        "  app:",
        "    build:",
        "      context: ./src",
        f"      dockerfile: {_q(dockerfile)}",
        "    restart: unless-stopped",
        "    env_file:",
        "      - path: ./src/.env",
        "        required: false",
    ]
    out += _labels_yaml(traefik_labels(name, domain, port), "    ")
    out += ["    networks:", f"      - {NETWORK}"]
    return "\n".join(out) + "\n" + _NETWORKS_FOOTER
