"""A frozen copy of the project's targets and boot pins: tests never follow upstream version bumps.

vendors/ is a symlink to the real vendor files, which hold logic rather than versions.
"""

from pathlib import Path

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "project"
