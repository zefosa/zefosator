"""A frozen copy of the project's targets and boot pins: tests never follow upstream version bumps.

vendors/ is a symlink to the real vendor files, which hold logic rather than versions. Tests must
not write into the fixture project: commands that write into the project root (e.g. cache/) get
a project_copy().
"""

import hashlib
import shutil
from pathlib import Path

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "project"


def project_copy(tmp_path: Path) -> Path:
    """A writable copy of the fixture project (vendor files copied, not linked)."""
    dest = tmp_path / "project"
    shutil.copytree(FIXTURE_ROOT, dest)
    return dest


def fixture_snapshot() -> dict[str, str]:
    return {
        path.relative_to(FIXTURE_ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(FIXTURE_ROOT.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }
