"""Optional HTTP basic auth in front of a deployment, enforced by a Traefik middleware.

Sites are public unless deployed with `--auth`. The credential is stored on the server only, as an
htpasswd line in ~/.qd/apps/<name>/htpasswd, so redeploys keep it until `--public` removes it.
Passwords are hashed with APR1-MD5 (Apache's htpasswd format, which Traefik accepts) because the
standard library has no bcrypt.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

from quick_deploy.errors import USAGE, QDError

# No ':' (htpasswd separator) or ',' (Traefik splits its user list on commas).
_USER_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
_HTPASSWD_RE = re.compile(r"^([A-Za-z0-9._@-]{1,64}):(\$apr1\$[./0-9A-Za-z]{1,8}\$[./0-9A-Za-z]{22})$")
_ITOA64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


@dataclass
class Auth:
    user: str
    htpasswd: str  # "user:$apr1$salt$hash"
    password: str | None = None  # plaintext, known only when this deploy set it
    generated: bool = False


def apr1(password: str, salt: str | None = None) -> str:
    """Apache's APR1-MD5 crypt, as produced by `htpasswd -m` or `openssl passwd -apr1`."""
    salt = (salt or "".join(secrets.choice(_ITOA64) for _ in range(8)))[:8]
    pw, s = password.encode(), salt.encode()
    alt = hashlib.md5(pw + s + pw).digest()
    ctx = pw + b"$apr1$" + s
    for n in range(len(pw), 0, -16):
        ctx += alt[: min(16, n)]
    n = len(pw)
    while n:
        ctx += b"\0" if n & 1 else pw[:1]
        n >>= 1
    final = hashlib.md5(ctx).digest()
    for i in range(1000):
        c = pw if i & 1 else final
        if i % 3:
            c += s
        if i % 7:
            c += pw
        c += final if i & 1 else pw
        final = hashlib.md5(c).digest()

    def to64(v: int, n: int) -> str:
        return "".join(_ITOA64[(v >> (6 * k)) & 0x3F] for k in range(n))

    out = "".join(to64(final[a] << 16 | final[b] << 8 | final[c], 4) for a, b, c in
                  ((0, 6, 12), (1, 7, 13), (2, 8, 14), (3, 9, 15), (4, 10, 5)))  # fmt: skip
    return f"$apr1${salt}${out}{to64(final[11], 2)}"


def _check_password(password: str, source: str) -> None:
    if any(ch in password for ch in "\r\n\0"):
        raise QDError(f"{source} can't contain newlines", USAGE)


def parse_spec(spec: str) -> tuple[str, str | None]:
    """`USER[:PASSWORD]` from --auth; the password may itself contain ':'."""
    user, sep, password = spec.partition(":")
    if not _USER_RE.match(user):
        raise QDError(f"invalid --auth user {user!r}: use 1-64 letters, digits and . _ @ -", USAGE)
    if sep and not password:
        raise QDError("empty --auth password; omit ':' to get a generated one", USAGE)
    _check_password(password, "the --auth password")
    return user, (password if sep else None)


def new_auth(spec: str, env_password: str | None = None) -> Auth:
    """Auth from --auth USER[:PASSWORD]; the password falls back to env_password, then a random one."""
    user, password = parse_spec(spec)
    generated = False
    if password is None and env_password:
        _check_password(env_password, "QD_AUTH_PASSWORD")
        password = env_password
    if password is None:
        password, generated = secrets.token_urlsafe(15), True
    return Auth(user, f"{user}:{apr1(password)}", password, generated)


def stored_auth(htpasswd: str, password: str | None = None) -> Auth:
    """Auth from the htpasswd line a previous deploy stored on the server.

    password (e.g. from QD_AUTH_PASSWORD) is kept only if it matches, so the site can be probed.
    """
    m = _HTPASSWD_RE.match(htpasswd.strip())
    if not m:
        raise QDError("the stored basic auth credential is malformed; redeploy with --auth or --public")
    salt = m[2].split("$")[2]
    if password and not hmac.compare_digest(apr1(password, salt), m[2]):
        password = None
    return Auth(m[1], m[0], password or None)
