from collections.abc import Sequence
from pathlib import Path

from zefosator.config import load_plan
from zefosator.podman import (
    UV_VERSION,
    build,
    image_build_argv,
    image_ref,
    render_containerfile,
    worker_run_argv,
)

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
