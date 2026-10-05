"""Local configuration, stored at ~/.config/qd/config.json (override with QD_CONFIG)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from quick_deploy.errors import USAGE, QDError


def config_path() -> Path:
    if p := os.environ.get("QD_CONFIG"):
        return Path(p)
    return Path.home() / ".config" / "qd" / "config.json"


@dataclass
class Config:
    host: str = ""  # ssh destination, e.g. me@homeserver.local or an ~/.ssh/config alias
    domain: str = ""  # e.g. example.com (a zone on your Cloudflare account)
    ssh_port: int | None = None
    ssh_key: str | None = None
    tunnel_name: str = "quick-deploy"
    tunnel_id: str | None = None

    @classmethod
    def load(cls) -> Config:
        path = config_path()
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise QDError(f"cannot parse {path}: {e}")
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n")
        path.chmod(0o600)

    def require(self) -> Config:
        if not self.host or not self.domain:
            raise QDError("not configured; run: qd setup --host USER@HOST --domain DOMAIN", USAGE)
        return self
