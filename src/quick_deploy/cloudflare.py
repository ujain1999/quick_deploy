"""Minimal Cloudflare API client for one-time tunnel + DNS provisioning.

Token permissions needed: Account > Cloudflare Tunnel > Edit, Zone > DNS > Edit,
Zone > Zone > Read (scoped to the domain's zone).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from quick_deploy import __version__
from quick_deploy.errors import QDError

API = "https://api.cloudflare.com/client/v4"
TRAEFIK_SERVICE = "http://qd-traefik:80"


class Cloudflare:
    def __init__(self, token: str):
        self.token = token

    def call(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(
            API + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": f"quick-deploy/{__version__}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                env = json.load(r)
        except urllib.error.HTTPError as e:
            try:
                env = json.load(e)
            except ValueError:
                raise QDError(f"cloudflare {method} {path}: HTTP {e.code}")
        except urllib.error.URLError as e:
            raise QDError(f"cloudflare {method} {path}: {e.reason}")
        if not env.get("success"):
            msgs = "; ".join(f"{x.get('code')}: {x.get('message')}" for x in env.get("errors") or [])
            raise QDError(f"cloudflare {method} {path}: {msgs or 'request failed'}")
        return env.get("result")

    def zone(self, domain: str) -> tuple[str, str]:
        """Return (zone_id, account_id) for domain."""
        zones = self.call("GET", f"/zones?name={urllib.parse.quote(domain)}")
        if not zones:
            raise QDError(f"zone {domain} not found (or the API token can't read it)")
        return zones[0]["id"], zones[0]["account"]["id"]

    def ensure_tunnel(self, account: str, name: str) -> str:
        """Find or create a remotely-managed tunnel; return its id."""
        q = urllib.parse.urlencode({"name": name, "is_deleted": "false"})
        found = self.call("GET", f"/accounts/{account}/cfd_tunnel?{q}")
        if found:
            return found[0]["id"]
        created = self.call("POST", f"/accounts/{account}/cfd_tunnel", {"name": name, "config_src": "cloudflare"})
        return created["id"]

    def tunnel_token(self, account: str, tunnel_id: str) -> str:
        return self.call("GET", f"/accounts/{account}/cfd_tunnel/{tunnel_id}/token")

    def put_ingress(self, account: str, tunnel_id: str, domain: str) -> None:
        """Send every *.domain request to Traefik on the server."""
        config = {
            "ingress": [
                {"hostname": f"*.{domain}", "service": TRAEFIK_SERVICE},
                {"service": "http_status:404"},
            ]
        }
        self.call("PUT", f"/accounts/{account}/cfd_tunnel/{tunnel_id}/configurations", {"config": config})

    def ensure_wildcard_dns(self, zone: str, domain: str, tunnel_id: str, force: bool) -> None:
        name = f"*.{domain}"
        target = f"{tunnel_id}.cfargotunnel.com"
        record = {
            "type": "CNAME",
            "name": name,
            "content": target,
            "proxied": True,
            "comment": "managed by quick-deploy (qd)",
        }
        existing = self.call("GET", f"/zones/{zone}/dns_records?{urllib.parse.urlencode({'name': name})}")
        if not existing:
            self.call("POST", f"/zones/{zone}/dns_records", record)
            return
        rec = existing[0]
        if rec["type"] == "CNAME" and rec["content"] == target and rec.get("proxied"):
            return
        if not force:
            raise QDError(
                f"DNS record {name} already exists ({rec['type']} {rec['content']}); "
                "rerun setup with --force to point it at the tunnel"
            )
        self.call("PUT", f"/zones/{zone}/dns_records/{rec['id']}", record)
