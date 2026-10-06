import shutil
import subprocess
from pathlib import Path

import pytest

from quick_deploy.remote import sync_filters


@pytest.mark.skipif(not shutil.which("rsync"), reason="needs rsync")
def test_sync_drops_excluded_env_but_keeps_other_excluded_files(tmp_path: Path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    (src / ".env").write_text("SECRET=1\n")
    (src / "app.py").write_text("")

    def sync() -> None:
        subprocess.run(["rsync", "-a", *sync_filters(src), f"{src}/", f"{dst}/"], check=True)

    sync()
    assert (dst / ".env").is_file()
    (dst / "data").mkdir()  # written on the server, e.g. by a bind mount
    (dst / "data" / "db").write_text("")
    (src / ".qdignore").write_text(".env\ndata/\n")
    sync()
    assert not (dst / ".env").exists()
    assert (dst / "data" / "db").is_file()
    assert (dst / "app.py").is_file()
