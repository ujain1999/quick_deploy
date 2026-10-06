"""Running commands on the server over ssh, and syncing files with rsync.

All server state lives under ~/.qd on the server:
  ~/.qd/system/          traefik + cloudflared compose project
  ~/.qd/apps/<name>/     one directory per deployment (src/, meta.json, generated compose file)
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

from quick_deploy.config import Config
from quick_deploy.errors import NOT_FOUND, QDError

# Prepended to every remote script. Scripts are POSIX sh (no bash needed on the server);
# pipefail is used where the shell supports it.
PRELUDE = 'set -e\n(set -o pipefail) 2>/dev/null && set -o pipefail\nQD="$HOME/.qd"\n'

DEFAULT_EXCLUDES = [
    ".git",
    ".DS_Store",
    "node_modules",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
]

shq = shlex.quote


def sync_filters(local_dir: Path) -> list[str]:
    """rsync options that mirror local_dir, minus DEFAULT_EXCLUDES and .qdignore patterns.

    Excluded files are kept on the server (so .qdignore can protect server-side data), except
    a top-level .env: once it's excluded or deleted locally it must stop reaching the container.
    """
    args = ["--delete", "--filter", "R /.env"]
    for ex in DEFAULT_EXCLUDES:
        args += ["--exclude", ex]
    ignore = local_dir / ".qdignore"
    if ignore.is_file():
        args += ["--exclude-from", str(ignore)]
    return args


def heredoc(path: str, content: str) -> str:
    """Shell snippet that writes content to path on the server."""
    if "\nQD_EOF\n" in f"\n{content}\n":
        raise ValueError("content contains heredoc terminator")
    return f"cat > {shq(path)} <<'QD_EOF'\n{content.rstrip(chr(10))}\nQD_EOF\n"


class Remote:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def ssh_opts(self) -> list[str]:
        opts = [
            "-o", "BatchMode=yes",  # never prompt; agents can't answer prompts
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15",
            # Reuse one connection across the several ssh calls a deploy makes.
            "-o", "ControlMaster=auto",
            "-o", "ControlPath=~/.ssh/qd-%C",
            "-o", "ControlPersist=120",
        ]  # fmt: skip
        if self.cfg.ssh_port:
            opts += ["-p", str(self.cfg.ssh_port)]
        if self.cfg.ssh_key:
            opts += ["-i", str(Path(self.cfg.ssh_key).expanduser())]
        return opts

    def _ssh(self, script: str, stdout, stderr) -> subprocess.CompletedProcess:
        sys.stderr.flush()
        return subprocess.run(
            ["ssh", *self.ssh_opts(), self.cfg.host, "sh -s"],
            input=(PRELUDE + script).encode(),
            stdout=stdout,
            stderr=stderr,
        )

    def _check(self, rc: int, err: str) -> None:
        err = err.strip()
        if rc == 0:
            return
        if rc == 255:
            raise QDError(
                f"ssh to {self.cfg.host} failed. Is the server on and reachable, and does "
                f"`ssh {self.cfg.host}` work without a password prompt?" + (f"\n{err}" if err else "")
            )
        if rc == NOT_FOUND:
            raise QDError(err or "deployment not found", NOT_FOUND)
        raise QDError(err or f"remote command failed (exit {rc})")

    def run(self, script: str, stdout=None) -> None:
        """Run a shell script on the server, streaming output (to stderr by default)."""
        p = self._ssh(script, stdout=stdout or sys.stderr, stderr=sys.stderr)
        self._check(p.returncode, "")

    def output(self, script: str) -> str:
        """Run a shell script on the server and return its stdout."""
        p = self._ssh(script, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._check(p.returncode, p.stderr.decode(errors="replace"))
        return p.stdout.decode(errors="replace")

    def sync(self, local_dir: Path, remote_dir: str) -> None:
        """Mirror local_dir into remote_dir (relative to the server user's home)."""
        args = ["rsync", "-az", "-e", "ssh " + " ".join(map(shq, self.ssh_opts()))]
        args += sync_filters(local_dir) + [f"{local_dir}/", f"{self.cfg.host}:{remote_dir}/"]
        sys.stderr.flush()
        p = subprocess.run(args, stdout=sys.stderr, stderr=sys.stderr)
        if p.returncode != 0:
            raise QDError(f"rsync to {self.cfg.host} failed (exit {p.returncode}); is rsync installed on the server?")
