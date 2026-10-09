"""Host side: build the per-target image and run the worker in it."""

import hashlib
import secrets
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from jinja2 import Environment, PackageLoader, StrictUndefined

from zefosator.config import Plan
from zefosator.errors import BuildError
from zefosator.signing import SigningInput
from zefosator.worker.boot import BootJob

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


SECRET_TARGET = "zefosa-signing-key"  # noqa: S105 -- a podman secret name, not a password
CERT_TARGET = "/signing/cert.pem"
CA_TARGET = "/signing/ca.der"


def worker_run_argv(  # noqa: PLR0913 -- one keyword per optional mount
    plan: Plan,
    ref: str,
    root: Path,
    *,
    secret: str | None = None,
    cert: Path | None = None,
    command: str = "build",
    job_file: str | None = None,
    volumes: tuple[str, ...] = (),
) -> list[str]:
    signing = []
    if secret is not None and cert is not None:
        signing = [
            f"--secret={secret},type=mount,target={SECRET_TARGET}",
            f"--volume={cert}:{CERT_TARGET}:ro",
        ]
    return [
        "podman",
        "run",
        "--rm",
        "--security-opt=label=disable",
        f"--volume={root}:/src:ro",
        f"--volume={root / 'cache'}:/cache",
        f"--volume={root / 'output' / plan.target_id}:/output",
        *volumes,
        *signing,
        "--env=PYTHONPATH=/src/src",
        ref,
        WORKER_PYTHON,
        "-m",
        "zefosator.worker",
        command,
        job_file or f"/cache/plans/{plan.target_id}.json",
    ]


class SecretStore(Protocol):
    def create(self, name: str, signing: SigningInput) -> None: ...

    def remove(self, name: str) -> None: ...


class PodmanSecrets:
    """Private keys reach containers only as podman secrets (never files in mounted dirs)."""

    def create(self, name: str, signing: SigningInput) -> None:
        if signing.key_file is not None:
            argv, data = ["podman", "secret", "create", name, str(signing.key_file)], None
        else:
            argv, data = ["podman", "secret", "create", name, "-"], (signing.key_pem or "").encode()
        result = subprocess.run(argv, input=data, capture_output=True, check=False)
        if result.returncode != 0:
            detail = result.stderr.decode(errors="replace")[-300:]
            msg = f"podman secret create {name} failed: {detail}"
            raise BuildError(msg)

    def remove(self, name: str) -> None:
        subprocess.run(["podman", "secret", "rm", name], capture_output=True, check=False)


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


def build(
    plan: Plan,
    root: Path,
    *,
    signing: SigningInput | None = None,
    execute: Executor = execute,
    secrets: SecretStore | None = None,
) -> None:
    ref = ensure_image(plan, root, execute=execute)
    plans = root / "cache" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / f"{plan.target_id}.json").write_text(plan.model_dump_json(indent=2))
    (root / "output" / plan.target_id).mkdir(parents=True, exist_ok=True)
    _run_signed(
        lambda secret: worker_run_argv(
            plan, ref, root, secret=secret, cert=signing.cert if signing else None
        ),
        signing,
        execute,
        secrets,
    )


def _run_signed(
    argv_for: Callable[[str | None], list[str]],
    signing: SigningInput | None,
    execute: Executor,
    secrets: SecretStore | None,
) -> None:
    """Run the worker; with signing, the key exists as a podman secret only during the run."""
    if signing is None:
        _check(argv_for(None), execute)
        return
    store = secrets or PodmanSecrets()
    name = f"zefosa-signing-{_token()}"
    store.create(name, signing)
    try:
        _check(argv_for(name), execute)
    finally:
        store.remove(name)


def build_boot(  # noqa: PLR0913 -- signing inputs and test seams are keyword-only
    job: BootJob,
    root: Path,
    *,
    signing: SigningInput | None,
    ca_cert: Path | None,
    execute: Executor = execute,
    secrets: SecretStore | None = None,
) -> None:
    plan = job.plan
    ref = ensure_image(plan, root, execute=execute)
    plans = root / "cache" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / f"{plan.target_id}.boot.json").write_text(job.model_dump_json(indent=2))
    (root / "output" / plan.target_id / "boot").mkdir(parents=True, exist_ok=True)
    (root / "output" / "boot").mkdir(parents=True, exist_ok=True)
    volumes = [f"--volume={root / 'output' / 'boot'}:/boot-output"]
    if ca_cert is not None:
        volumes.append(f"--volume={ca_cert}:{CA_TARGET}:ro")
    _run_signed(
        lambda secret: worker_run_argv(
            plan,
            ref,
            root,
            secret=secret,
            cert=signing.cert if signing else None,
            command="boot",
            job_file=f"/cache/plans/{plan.target_id}.boot.json",
            volumes=tuple(volumes),
        ),
        signing,
        execute,
        secrets,
    )


def _token() -> str:
    return secrets.token_hex(6)
