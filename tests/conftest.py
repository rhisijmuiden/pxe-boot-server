"""Test setup: run the app in dev mode with all data in a temporary directory."""
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="pxetest-"))
os.environ.update({
    "PXE_DEV": "1",
    "PXE_DATA_DIR": str(_tmp / "data"),
    "PXE_SRV_DIR": str(_tmp / "srv"),
    "PXE_LOG_DIR": str(_tmp / "log"),
    "PXE_NGINX_LOG": str(_tmp / "log" / "access.log"),
    "PXE_ETC_DIR": str(_tmp / "etc"),
})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from app import config, db  # noqa: E402

db.migrate()


def make_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root


@pytest.fixture
def tree(tmp_path):
    return lambda files: make_tree(tmp_path / "iso", files)


@pytest.fixture
def iso_row():
    """Insert an ISO with a fake mounted tree and run detection on it."""
    from app import isos

    counter = {"n": 0}

    def _make(files: dict[str, str], relpath: str | None = None):
        counter["n"] += 1
        relpath = relpath or f"test-{os.getpid()}-{counter['n']}-{len(files)}.iso"
        (config.ISO_DIR / relpath).parent.mkdir(parents=True, exist_ok=True)
        (config.ISO_DIR / relpath).write_bytes(b"\0" * 2048)
        iso_id = isos.add_file(relpath)
        make_tree(config.MNT_DIR / str(iso_id), files)
        isos.process(iso_id)
        return isos.get(iso_id)

    return _make
