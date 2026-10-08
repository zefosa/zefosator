"""Host side: build the per-target image and run the worker in it."""

import hashlib
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from jinja2 import Environment, PackageLoader, StrictUndefined

from zefosator.config import Plan
from zefosator.errors import BuildError

UV_VERSION = "0.12.19"
PROJECT_FILES = ("pyproject.toml", "uv.lock", ".python-version")
WORKER_PYTHON = "/opt/venv/bin/python"

_ENV = Environment(
    loader=PackageLoader("zefosator", "templates"),
    undefined=StrictUndefined,
    autoescape=False,  # noqa: S701 -- renders a Containerfile, not HTML
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
)

Executor = Callable[[Sequence[str]], int]


def execute(argv: Sequence[str]) -> int:
    """Run with output going straight to the terminal; return the exit code."""
    try:
        return subprocess.run(list(argv), check=False).returncode
    except OSError as exc:
        msg = f"cannot run {argv[0]}: {exc.strerror or exc}"
        raise BuildError(msg) from exc


def render_containerfile(plan: Plan) -> str:
    return _ENV.get_template("Containerfile.j2").render(image=plan.image, uv_version=UV_VERSION)


def image_ref(plan: Plan, containerfile: str, lock: bytes) -> str:
    digest = hashlib.sha256(containerfile.encode() + b"\0" + lock).hexdigest()[:16]
    return f"localhost/zefosator-{plan.vendor}-{plan.os_major}-{plan.arch}:{digest}"


def image_build_argv(ref: str, context: Path) -> list[str]:
    return ["podman", "build", "--tag", ref, "--file", str(context / "Containerfile"), str(context)]


def worker_run_argv(plan: Plan, ref: str, root: Path) -> list[str]:
    return [
        "podman",
        "run",
        "--rm",
        "--security-opt=label=disable",
        f"--volume={root}:/src:ro",
        f"--volume={root / 'cache'}:/cache",
        f"--volume={root / 'output' / plan.target_id}:/output",
        "--env=PYTHONPATH=/src/src",
        ref,
        WORKER_PYTHON,
        "-m",
        "zefosator.worker",
        "build",
        f"/cache/plans/{plan.target_id}.json",
    ]


def _check(argv: Sequence[str], run: Executor) -> None:
    code = run(argv)
    if code != 0:
        msg = f"command failed with exit code {code}: {' '.join(argv)}"
        raise BuildError(msg)


def ensure_image(plan: Plan, root: Path, *, execute: Executor = execute) -> str:
    containerfile = render_containerfile(plan)
    lock = b"\0".join((root / name).read_bytes() for name in PROJECT_FILES)
    ref = image_ref(plan, containerfile, lock)
    if execute(["podman", "image", "exists", ref]) == 0:
        return ref
    context = root / "cache" / "image-context" / plan.target_id
    shutil.rmtree(context, ignore_errors=True)
    context.mkdir(parents=True)
    for name in PROJECT_FILES:
        shutil.copy2(root / name, context / name)
    (context / "Containerfile").write_text(containerfile)
    _check(image_build_argv(ref, context), execute)
    return ref


def build(plan: Plan, root: Path, *, execute: Executor = execute) -> None:
    ref = ensure_image(plan, root, execute=execute)
    plans = root / "cache" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / f"{plan.target_id}.json").write_text(plan.model_dump_json(indent=2))
    (root / "output" / plan.target_id).mkdir(parents=True, exist_ok=True)
    _check(worker_run_argv(plan, ref, root), execute)
