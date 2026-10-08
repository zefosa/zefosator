import subprocess
import sys

CHECKS: tuple[tuple[str, ...], ...] = (
    ("ruff", "format", "--check"),
    ("ruff", "check"),
    ("mypy",),
    ("pytest", "-q"),
)


def main() -> int:
    for argv in CHECKS:
        print(f"==> {' '.join(argv)}", flush=True)
        if subprocess.run(argv, check=False).returncode != 0:
            print(f"FAILED: {' '.join(argv)}", file=sys.stderr)
            return 1
    return 0
