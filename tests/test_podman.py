from collections.abc import Sequence
from pathlib import Path

import pytest

from zefosator.config import SigningInfo, load_boot_config, load_plan
from zefosator.errors import BuildError
from zefosator.podman import (
    UV_VERSION,
    build,
    build_boot,
    image_build_argv,
    image_ref,
    render_containerfile,
    worker_run_argv,
)
from zefosator.signing import SigningInput
from zefosator.worker.boot import BootJob

ROOT = Path(__file__).resolve().parent.parent
FEDORA = load_plan(ROOT / "targets" / "fedora-44-x86_64.yaml")
ALMA = load_plan(ROOT / "targets" / "almalinux-9-x86_64.yaml")


def test_fedora_containerfile() -> None:
    text = render_containerfile(FEDORA)
    assert text.startswith("FROM docker.io/library/fedora:44\n")
    assert "epel" not in text
    assert "RUN dnf install -y autoconf automake" in text
    assert f"COPY --from=ghcr.io/astral-sh/uv:{UV_VERSION} /uv /usr/local/bin/uv" in text
    assert "RUN uv sync --frozen --no-install-project --no-dev" in text


def test_almalinux_containerfile_installs_setup_packages_and_enables_repos() -> None:
    text = render_containerfile(ALMA)
    assert "FROM docker.io/almalinux/9-base\n" in text
    setup = text.index("RUN dnf install -y epel-release")
    build_deps = text.index("RUN dnf install -y --enablerepo=crb --enablerepo=epel autoconf")
    assert setup < build_deps


def test_image_ref_depends_on_containerfile_and_lock() -> None:
    ref = image_ref(FEDORA, "FROM x", b"lock")
    assert ref.startswith("localhost/zefosator-fedora-44-x86_64:")
    assert ref == image_ref(FEDORA, "FROM x", b"lock")
    assert ref != image_ref(FEDORA, "FROM x", b"lock2")
    assert ref != image_ref(FEDORA, "FROM y", b"lock")


def test_image_build_argv(tmp_path: Path) -> None:
    argv = image_build_argv("localhost/img:1", tmp_path)
    assert argv == [
        "podman",
        "build",
        "--tag",
        "localhost/img:1",
        "--file",
        str(tmp_path / "Containerfile"),
        str(tmp_path),
    ]


def test_worker_run_argv(tmp_path: Path) -> None:
    argv = worker_run_argv(ALMA, "localhost/img:1", tmp_path)
    assert argv[:4] == ["podman", "run", "--rm", "--security-opt=label=disable"]
    assert f"--volume={tmp_path}:/src:ro" in argv
    assert f"--volume={tmp_path / 'cache'}:/cache" in argv
    assert f"--volume={tmp_path / 'output' / ALMA.target_id}:/output" in argv
    assert "--env=PYTHONPATH=/src/src" in argv
    assert argv[-6:] == [
        "localhost/img:1",
        "/opt/venv/bin/python",
        "-m",
        "zefosator.worker",
        "build",
        f"/cache/plans/{ALMA.target_id}.json",
    ]


class FakePodman:
    def __init__(self, *, image_exists: bool) -> None:
        self.image_exists = image_exists
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> int:
        self.calls.append(list(argv))
        if list(argv[:3]) == ["podman", "image", "exists"]:
            return 0 if self.image_exists else 1
        return 0


def make_root(tmp_path: Path) -> Path:
    for name in ("pyproject.toml", "uv.lock", ".python-version"):
        (tmp_path / name).write_text(name)
    return tmp_path


def test_build_creates_image_when_missing_and_runs_worker(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    podman = FakePodman(image_exists=False)
    build(FEDORA, root, execute=podman)
    commands = [c[:2] for c in podman.calls]
    assert commands == [["podman", "image"], ["podman", "build"], ["podman", "run"]]
    context = Path(podman.calls[1][-1])
    assert (context / "uv.lock").read_text() == "uv.lock"
    assert (context / "Containerfile").read_text() == render_containerfile(FEDORA)
    plan_file = root / "cache" / "plans" / f"{FEDORA.target_id}.json"
    assert plan_file.is_file()
    assert (root / "output" / FEDORA.target_id).is_dir()


def test_build_reuses_existing_image(tmp_path: Path) -> None:
    podman = FakePodman(image_exists=True)
    build(FEDORA, make_root(tmp_path), execute=podman)
    assert [c[:2] for c in podman.calls] == [["podman", "image"], ["podman", "run"]]


class FakeSecrets:
    def __init__(self) -> None:
        self.created: list[tuple[str, SigningInput]] = []
        self.removed: list[str] = []

    def create(self, name: str, signing: SigningInput) -> None:
        self.created.append((name, signing))

    def remove(self, name: str) -> None:
        self.removed.append(name)


def test_signed_build_passes_key_as_secret_and_removes_it(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    cert = tmp_path / "signing.pem"
    cert.write_text("cert")
    signing = SigningInput(cert=cert, key_pem="-----BEGIN PRIVATE KEY-----secret")
    plan = FEDORA.model_copy(
        update={"signing": SigningInfo(cert_sha256="ab" * 32, cert_subject="CN=x")}
    )
    secrets = FakeSecrets()
    podman = FakePodman(image_exists=True)
    build(plan, root, signing=signing, execute=podman, secrets=secrets)
    name = secrets.created[0][0]
    assert name.startswith("zefosa-signing-")
    run = podman.calls[-1]
    assert f"--secret={name},type=mount,target=zefosa-signing-key" in run
    assert f"--volume={cert}:/signing/cert.pem:ro" in run
    assert not any("secret" in a for a in run if "PRIVATE" in a)
    assert secrets.removed == [name]
    assert "PRIVATE" not in (root / "cache" / "plans" / f"{plan.target_id}.json").read_text()


def test_secret_removed_when_worker_fails(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    cert = tmp_path / "signing.pem"
    cert.write_text("cert")
    secrets = FakeSecrets()

    def failing(argv: Sequence[str]) -> int:
        return 0 if list(argv[:3]) == ["podman", "image", "exists"] else 3

    with pytest.raises(BuildError):
        build(
            FEDORA,
            root,
            signing=SigningInput(cert=cert, key_file=cert),
            execute=failing,
            secrets=secrets,
        )
    assert len(secrets.removed) == 1


def test_unsigned_build_has_no_secret(tmp_path: Path) -> None:
    secrets = FakeSecrets()
    podman = FakePodman(image_exists=True)
    build(FEDORA, make_root(tmp_path), signing=None, execute=podman, secrets=secrets)
    assert secrets.created == []
    assert not [a for a in podman.calls[-1] if a.startswith("--secret")]


def boot_job(target: str) -> BootJob:
    plan = load_plan(ROOT / "targets" / f"{target}.yaml")
    return BootJob(plan=plan, config=load_boot_config(ROOT))


def test_build_boot_common_mounts_signing_material(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    cert, ca = tmp_path / "signing.pem", tmp_path / "ca.der"
    cert.write_text("c")
    ca.write_text("a")
    secrets = FakeSecrets()
    podman = FakePodman(image_exists=True)
    job = boot_job("fedora-44-x86_64")
    build_boot(job, root, signing=SigningInput(cert=cert, key_file=cert), ca_cert=ca,
               execute=podman, secrets=secrets)  # fmt: skip
    run = podman.calls[-1]
    tid = job.plan.target_id
    assert f"--volume={root / 'output' / tid}:/output" in run
    assert f"--volume={root / 'output' / 'boot'}:/boot-output" in run
    assert f"--volume={ca}:/signing/ca.der:ro" in run
    assert any(a.startswith("--secret=zefosa-signing-") for a in run)
    assert run[-2:] == ["boot", f"/cache/plans/{tid}.boot.json"]
    saved = BootJob.model_validate_json((root / "cache" / "plans" / f"{tid}.boot.json").read_text())
    assert saved == job
    assert len(secrets.removed) == 1


def test_build_boot_shim_only_has_no_secrets(tmp_path: Path) -> None:
    secrets = FakeSecrets()
    podman = FakePodman(image_exists=True)
    build_boot(boot_job("almalinux-9-x86_64"), make_root(tmp_path), signing=None, ca_cert=None,
               execute=podman, secrets=secrets)  # fmt: skip
    assert secrets.created == []
    assert not [a for a in podman.calls[-1] if "/signing/" in a or a.startswith("--secret")]
