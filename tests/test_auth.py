import base64
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from quick_deploy.auth import apr1, new_auth, parse_spec, stored_auth
from quick_deploy.cli import deploy_auth
from quick_deploy.errors import QDError
from quick_deploy.server import App, probe


@pytest.mark.parametrize(
    "password, salt, expected",
    [  # from `openssl passwd -apr1 -salt SALT PASSWORD`
        ("password", "saltsalt", "$apr1$saltsalt$yAAkm4libquA.ZWLHbSBq/"),
        ("a much longer password over 16 bytes!", "ab/CD.9z", "$apr1$ab/CD.9z$V/u84jILXMlvKcRcLopDl/"),
        ("pässwörd", "x", "$apr1$x$Kzn.cSDdCrYQZfFg5UXZM."),
        ("p", "12345678", "$apr1$12345678$XsuVNBMt1BhIqqVcUl6NI1"),
    ],
)
def test_apr1_matches_openssl(password, salt, expected):
    assert apr1(password, salt) == expected


def test_apr1_random_salt():
    a, b = apr1("pw"), apr1("pw")
    assert a != b and a.startswith("$apr1$") and len(a.split("$")[2]) == 8


@pytest.mark.parametrize(
    "spec, expected",
    [
        ("alice", ("alice", None)),
        ("alice:s3cret", ("alice", "s3cret")),
        ("bob@example.com:a:b:c", ("bob@example.com", "a:b:c")),
    ],
)
def test_parse_spec(spec, expected):
    assert parse_spec(spec) == expected


@pytest.mark.parametrize("spec", ["", ":pw", "al ice", "a,b", "alice:", "a$b", "alice:x\ny"])
def test_parse_spec_rejects(spec):
    with pytest.raises(QDError) as e:
        parse_spec(spec)
    assert e.value.code == 2


def test_new_auth_password_sources():
    given = new_auth("alice:pw", "ignored")
    assert (given.user, given.password, given.generated) == ("alice", "pw", False)
    assert given.htpasswd.startswith("alice:$apr1$")
    salt = given.htpasswd.split("$")[2]
    assert given.htpasswd == f"alice:{apr1('pw', salt)}"

    env = new_auth("alice", "from-env")
    assert (env.password, env.generated) == ("from-env", False)

    gen = new_auth("alice", None)
    assert gen.generated and len(gen.password) >= 16


def test_stored_auth_roundtrip():
    a = new_auth("alice:pw")
    s = stored_auth(a.htpasswd + "\n")
    assert (s.user, s.htpasswd, s.password) == ("alice", a.htpasswd, None)
    assert stored_auth(a.htpasswd, "pw").password == "pw"
    assert stored_auth(a.htpasswd, "nope").password is None
    with pytest.raises(QDError):
        stored_auth("alice:plaintext")


def app(auth=None):
    return App("blog", "https://blog.d", "/src", "mac", "compose", "web", 80, "", auth=auth)


def no_read():
    raise AssertionError("should not read the server")


def resolve(spec=None, public=False, prev=None, owner_changed=False, read=no_read, env=None):
    return deploy_auth(spec, public, prev, owner_changed, read, env)


def test_deploy_auth_default_is_public():
    assert resolve() is None
    assert resolve(prev=app()) is None


def test_deploy_auth_redeploy_keeps_auth():
    stored = new_auth("alice:pw").htpasswd
    kept = resolve(prev=app("alice"), read=lambda: stored + "\n")
    assert (kept.user, kept.htpasswd, kept.password) == ("alice", stored, None)


def test_deploy_auth_kept_auth_probes_with_matching_env_password():
    stored = new_auth("alice:pw").htpasswd
    assert resolve(prev=app("alice"), read=lambda: stored, env="pw").password == "pw"
    assert resolve(prev=app("alice"), read=lambda: stored, env="wrong").password is None


def test_deploy_auth_public_removes_and_auth_replaces():
    assert resolve(public=True, prev=app("alice")) is None
    assert resolve("bob:pw", prev=app("alice")).user == "bob"


def test_deploy_auth_empty_spec_is_an_error():
    with pytest.raises(QDError) as e:
        resolve("")
    assert e.value.code == 2


def test_deploy_auth_missing_credential_fails_closed():
    with pytest.raises(QDError, match="--auth or --public"):
        resolve(prev=app("alice"), read=lambda: "")


def test_deploy_auth_takeover_needs_explicit_choice():
    with pytest.raises(QDError, match="--auth or --public") as e:
        resolve(prev=app("alice"), owner_changed=True)
    assert e.value.code == 2
    assert resolve(prev=app(), owner_changed=True) is None
    assert resolve("bob", prev=app("alice"), owner_changed=True).user == "bob"


def test_env_password_errors_name_the_env_var():
    with pytest.raises(QDError, match="QD_AUTH_PASSWORD"):
        new_auth("alice", "pw\r\n")


def test_probe_sends_basic_auth():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        assert probe(url, auth=("alice", "pä:ss")) == (200, "ok", "")
        assert probe(url) == (200, "ok", "")
    finally:
        server.shutdown()
    assert seen == ["Basic " + base64.b64encode("alice:pä:ss".encode()).decode(), None]
