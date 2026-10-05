import json
import shutil
import subprocess
from pathlib import Path

import pytest

from quick_deploy.errors import QDError
from quick_deploy.names import random_name
from quick_deploy.project import (
    build_expose,
    compose_override,
    detect_project,
    dockerfile_compose,
    parse_expose,
    pick_service,
    remote_to_local,
    tunnel_services,
    valid_name,
)
from quick_deploy.server import app_is_up, version_at_least


def test_random_name_shape():
    for _ in range(200):
        n = random_name()
        assert len(n.split("-")) == 3 and valid_name(n)


def test_random_name_avoids_taken(monkeypatch):
    import quick_deploy.names as names

    monkeypatch.setattr(names, "ADJECTIVES", ["a", "b"])
    monkeypatch.setattr(names, "NOUNS", ["x"])
    assert random_name({"a-b-x"}) == "b-a-x"


@pytest.mark.parametrize("name,ok", [("blog", True), ("my-app-2", True), ("-x", False), ("a.b", False), ("Blog", False), ("x" * 64, False)])
def test_valid_name(name, ok):
    assert valid_name(name) is ok


def test_parse_expose_uses_last_stage():
    df = "FROM node AS build\nEXPOSE 9999\nFROM nginx\nexpose 8080/tcp 9090\n"
    assert parse_expose(df) == 8080
    assert parse_expose("FROM a\nEXPOSE 1\nFROM b\n") is None


def test_parse_expose_target_stage():
    df = "FROM --platform=$BUILDPLATFORM node AS Build\nEXPOSE 5173\nFROM nginx AS prod\nEXPOSE 80\n"
    assert parse_expose(df, "build") == 5173
    assert parse_expose(df, "prod") == 80
    assert parse_expose(df) == 80


def test_remote_to_local(tmp_path: Path):
    root = "/.qd/apps/x/src"
    assert remote_to_local("/home/u/.qd/apps/x/src", root, tmp_path) == tmp_path
    assert remote_to_local("/home/u/.qd/apps/x/src/web/Dockerfile", root, tmp_path) == tmp_path / "web/Dockerfile"
    assert remote_to_local("/home/u/.qd/apps/x/src2", root, tmp_path) is None
    assert remote_to_local("https://github.com/a/b.git", root, tmp_path) is None


def test_build_expose(tmp_path: Path):
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker" / "Dockerfile").write_text("FROM python\nEXPOSE 8000\n")
    to_local = lambda p: remote_to_local(p, "/srv/src", tmp_path)  # noqa: E731
    assert build_expose({"build": {"context": "/srv/src", "dockerfile": "docker/Dockerfile"}}, to_local) == 8000
    assert build_expose({"build": {"context": "/srv/src", "dockerfile": "/srv/src/docker/Dockerfile"}}, to_local) == 8000
    assert build_expose({"build": {"context": "/srv/src"}}, to_local) is None  # no ./Dockerfile
    assert build_expose({"build": {"dockerfile_inline": "FROM x\nEXPOSE 9000"}}, to_local) == 9000
    assert build_expose({"image": "nginx"}, to_local) is None


def test_pick_service_falls_back_to_dockerfile():
    services = {"app": {"build": {}}, "db": {"image": "postgres:16"}}
    assert pick_service(services, None, None, lambda spec: 8000) == ("app", 8000)
    assert pick_service(services, None, 9000, lambda spec: 8000) == ("app", 9000)
    with pytest.raises(QDError, match="Dockerfile EXPOSE"):
        pick_service(services, None, None, lambda spec: None)


def test_tunnel_services():
    services = {
        "app": {"build": {}},
        "cloudflared": {"image": "cloudflare/cloudflared:latest"},
        "tun": {"image": "docker.io/cloudflare/cloudflared@sha256:abc"},
        "other": {"image": "registry:5000/cloudflared-exporter"},
    }
    assert tunnel_services(services) == ["cloudflared", "tun"]


def test_detect_prefers_compose(tmp_path: Path):
    (tmp_path / "Dockerfile").write_text("FROM x\nEXPOSE 3000\n")
    assert detect_project(tmp_path).mode == "dockerfile"
    assert detect_project(tmp_path).exposed_port == 3000
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    assert detect_project(tmp_path).mode == "compose"
    assert detect_project(tmp_path, "Dockerfile").mode == "dockerfile"
    with pytest.raises(QDError):
        detect_project(tmp_path, "nope.yml")


SERVICES = {
    "db": {"image": "postgres", "ports": [{"target": 5432, "published": "5432"}]},
    "web": {"build": {}, "expose": ["3000"], "networks": {"default": None}},
    "worker": {"image": "x", "restart": "always"},
}


def test_pick_service():
    assert pick_service(SERVICES, None, None) == ("web", 3000)  # db publishes a port but is a datastore
    assert pick_service(SERVICES, "db", None) == ("db", 5432)
    assert pick_service(SERVICES, "web", 8000) == ("web", 8000)
    assert pick_service({"only": {"ports": [{"target": 80}]}}, None, None) == ("only", 80)
    with pytest.raises(QDError):
        pick_service({"a": {}, "b": {}}, None, None)
    with pytest.raises(QDError):
        pick_service({"a": {}}, None, None)  # no port


def test_pick_service_unique_published_port_wins():
    services = {"api": {"expose": ["9000"]}, "ui": {"ports": [{"target": 80}]}, "cache": {"image": "redis:7"}}
    assert pick_service(services, None, None) == ("ui", 80)


def test_pick_service_single_non_datastore():
    services = {"blog": {"expose": ["2368"]}, "db": {"image": "docker.io/library/mysql:8", "ports": [{"target": 3306}]}}
    assert pick_service(services, None, None) == ("blog", 2368)


def test_app_is_up():
    assert app_is_up(200, "hi")
    assert app_is_up(404, "custom not found")
    assert app_is_up(302, "")
    assert not app_is_up(404, "404 page not found\n")
    assert not app_is_up(530, "")
    assert not app_is_up(502, "")
    assert not app_is_up(None, "")


def test_version_at_least():
    assert version_at_least("2.24.0", (2, 24))
    assert version_at_least("v2.30.1", (2, 24))
    assert version_at_least("5.3.1", (2, 24))
    assert not version_at_least("2.23.9", (2, 24))
    assert not version_at_least("", (2, 24))


# --- real compose merge semantics (needs the docker CLI; no daemon) -------------------

needs_compose = pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")


def compose_config(cwd: Path, *files: str) -> dict:
    args = ["docker", "compose", "-p", "qd-test"]
    for f in files:
        args += ["-f", f]
    out = subprocess.run(args + ["config", "--format", "json"], cwd=cwd, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@needs_compose
def test_override_merges_with_user_compose(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "compose.yml").write_text(
        """
services:
  web:
    build: .
    ports: ["8080:3000"]
    labels: ["user.label=kept"]
    depends_on: [db]
  db:
    image: postgres
    ports: ["5432:5432"]
    networks: [backend]
    restart: always
networks:
  backend: {}
"""
    )
    services = compose_config(src, "compose.yml")["services"]
    svc, port = pick_service(services, None, None)
    assert (svc, port) == ("web", 3000)
    (tmp_path / "qd.override.yml").write_text(compose_override(services, svc, port, "blog", "isalive.win"))

    merged = compose_config(tmp_path, "src/compose.yml", "qd.override.yml")["services"]
    web, db = merged["web"], merged["db"]
    assert not web.get("ports") and not db.get("ports")
    assert web["labels"]["user.label"] == "kept"
    assert web["labels"]["traefik.http.routers.qd-blog.rule"] == "Host(`blog.isalive.win`)"
    assert web["labels"]["traefik.http.services.qd-blog.loadbalancer.server.port"] == "3000"
    assert set(web["networks"]) == {"default", "qd"}
    assert set(db["networks"]) == {"backend"}
    assert web["restart"] == "unless-stopped" and db["restart"] == "always"
    # build context still resolves relative to the user's compose file
    assert Path(web["build"]["context"]) == src.resolve()


@needs_compose
def test_override_keep_ports(tmp_path: Path):
    (tmp_path / "compose.yml").write_text("services:\n  web:\n    image: nginx\n    ports: ['8080:80']\n")
    services = compose_config(tmp_path, "compose.yml")["services"]
    (tmp_path / "o.yml").write_text(compose_override(services, "web", 80, "x", "d.com", keep_ports=True))
    assert compose_config(tmp_path, "compose.yml", "o.yml")["services"]["web"]["ports"]


@needs_compose
def test_dockerfile_compose_is_valid(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Dockerfile").write_text("FROM nginx\n")
    (tmp_path / "src" / ".env").write_text("FOO=bar\n")
    (tmp_path / "qd.compose.yml").write_text(dockerfile_compose("blog", "isalive.win", "Dockerfile", 80, True))
    app = compose_config(tmp_path, "qd.compose.yml")["services"]["app"]
    assert app["environment"]["FOO"] == "bar"
    assert app["labels"]["traefik.enable"] == "true"
    assert list(app["networks"]) == ["qd"]


@needs_compose
def test_compose_build_port_from_dockerfile(tmp_path: Path):
    """The xlri-schedule-sync shape: app builds docker/Dockerfile, no ports/expose, plus its own tunnel."""
    remote_src = tmp_path / "home" / ".qd" / "apps" / "demo" / "src"
    local_src = tmp_path / "local"
    for root in (remote_src, local_src):
        (root / "docker").mkdir(parents=True)
        (root / "docker" / "Dockerfile").write_text("FROM python:3.12-slim\nEXPOSE 8000\n")
        (root / "docker-compose.yml").write_text(
            "services:\n"
            "  db: {image: 'postgres:16-alpine'}\n"
            "  app: {build: {context: ., dockerfile: docker/Dockerfile}, depends_on: [db]}\n"
            "  cloudflared: {image: 'cloudflare/cloudflared:latest', command: tunnel run}\n"
        )
    services = compose_config(remote_src, "docker-compose.yml")["services"]
    to_local = lambda p: remote_to_local(p, "/.qd/apps/demo/src", local_src)  # noqa: E731
    assert pick_service(services, None, None, lambda spec: build_expose(spec, to_local)) == ("app", 8000)
    assert tunnel_services(services) == ["cloudflared"]
