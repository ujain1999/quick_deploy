import shutil
import subprocess

import pytest

from quick_deploy.cli import _exists_check
from quick_deploy.remote import PRELUDE, heredoc
from quick_deploy.server import _DETECT_SOCK, LIST_SCRIPT, STATUS_SCRIPT, SYSTEM_COMPOSE, docker_hint, pkg_hint


@pytest.mark.parametrize(
    "os_ids, expected",
    [
        ("fedora", "sudo dnf install -y rsync"),
        ("ubuntu debian", "sudo apt-get install -y rsync"),
        ("raspbian debian", "sudo apt-get install -y rsync"),
        ("rocky rhel centos fedora", "sudo dnf install -y rsync"),
        ("manjaro arch", "sudo pacman -S --needed rsync"),
        ("opensuse-tumbleweed opensuse suse", "sudo zypper install -y rsync"),
        ("alpine", "sudo apk add rsync"),
        ("", "install rsync with the server's package manager"),
    ],
)
def test_pkg_hint(os_ids, expected):
    assert pkg_hint(os_ids, "rsync") == expected


def test_docker_hint():
    assert "moby-engine" in docker_hint("fedora")
    # RHEL clones list fedora in ID_LIKE but have no moby-engine package.
    assert "get.docker.com" in docker_hint("rocky rhel centos fedora")
    assert "addgroup" in docker_hint("alpine")
    assert "docs.docker.com" in docker_hint("") and "usermod" in docker_hint("")


SHELLS = [s for s in ("sh", "dash", "busybox") if shutil.which(s)]
SCRIPTS = {
    "status": STATUS_SCRIPT,
    "list": LIST_SCRIPT,
    "bootstrap": _DETECT_SOCK + heredoc("compose.yml", SYSTEM_COMPOSE),
    "logs": _exists_check("x") + "docker compose -p qd-x logs\n",
}


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("script", sorted(SCRIPTS))
def test_remote_scripts_are_posix(shell, script):
    """The server may not have bash (e.g. Alpine), so scripts must parse in a plain sh."""
    cmd = [shell, "sh", "-n"] if shell == "busybox" else [shell, "-n"]
    p = subprocess.run(cmd, input=(PRELUDE + SCRIPTS[script]).encode(), capture_output=True)
    assert p.returncode == 0, p.stderr.decode()


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(
    "context_host, expected",
    [
        ("unix:///run/user/1000/docker.sock", "/run/user/1000/docker.sock"),  # rootless
        ("unix:///var/run/docker.sock", "/var/run/docker.sock"),
        ("", "/var/run/docker.sock"),  # no docker context support
    ],
)
def test_detect_sock(shell, context_host, expected, tmp_path):
    fake = tmp_path / "docker"
    fake.write_text(f"#!/bin/sh\n[ -n '{context_host}' ] && echo '{context_host}' || exit 1\n")
    fake.chmod(0o755)
    cmd = [shell, "sh", "-s"] if shell == "busybox" else [shell, "-s"]
    script = f'PATH="{tmp_path}:$PATH"\n' + PRELUDE + _DETECT_SOCK + 'echo "$sock"\n'
    p = subprocess.run(cmd, input=script.encode(), capture_output=True)
    assert p.stdout.decode().strip() == expected, p.stderr.decode()
