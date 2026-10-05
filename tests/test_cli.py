import json

import pytest

from quick_deploy import cli


def run(capsys, *argv):
    with pytest.raises(SystemExit) as e:
        cli.main(list(argv))
    out, err = capsys.readouterr()
    return e.value.code, out, err


def test_json_anywhere(capsys):
    code, out, _ = run(capsys, "name", "--json")
    assert code == 0 and len(json.loads(out)["name"].split("-")) == 3


def test_not_configured_is_usage_error(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("QD_CONFIG", str(tmp_path / "none.json"))
    code, out, _ = run(capsys, "--json", "ls")
    assert code == 2 and json.loads(out)["ok"] is False


def test_bad_flag_reports_json(capsys):
    code, out, _ = run(capsys, "deploy", "--bogus", "--json")
    assert code == 2 and "unrecognized" in json.loads(out)["error"]
