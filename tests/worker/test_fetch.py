from pathlib import Path

import pytest

from tests.project import FIXTURE_ROOT
from zefosator.config import load_plan
from zefosator.errors import BuildError
from zefosator.worker.fetch import fetch_all, fetch_file, sha256_verifier

PLAN = load_plan(FIXTURE_ROOT / "targets" / "almalinux-9-x86_64.yaml")


class FakeNet:
    def __init__(self, failures: int = 0) -> None:
        self.calls: list[str] = []
        self.failures = failures

    def download(self, url: str, dest: Path) -> None:
        self.calls.append(url)
        if self.failures:
            self.failures -= 1
            dest.write_bytes(b"partial")
            msg = "connection reset"
            raise OSError(msg)
        dest.write_bytes(b"good:" + url.encode())


def verify_good(path: Path) -> None:
    if not path.read_bytes().startswith(b"good:"):
        msg = f"{path.name}: digests NOT OK"
        raise BuildError(msg)


def test_downloads_every_package_and_returns_paths(tmp_path: Path) -> None:
    net = FakeNet()
    paths = fetch_all(
        PLAN, tmp_path, download=net.download, verify=verify_good, sleep=lambda _: None
    )
    assert set(paths) == {p.name for p in PLAN.packages}
    assert paths["kernel-core"] == tmp_path / PLAN.role("core").file
    assert net.calls == [p.url for p in PLAN.packages]
    assert not list(tmp_path.glob("*.part"))


def test_valid_cached_files_are_not_downloaded_again(tmp_path: Path) -> None:
    for pkg in PLAN.packages:
        (tmp_path / pkg.file).write_bytes(b"good:cached")
    net = FakeNet()
    fetch_all(PLAN, tmp_path, download=net.download, verify=verify_good, sleep=lambda _: None)
    assert net.calls == []


def test_invalid_cached_file_is_downloaded_again(tmp_path: Path) -> None:
    core = PLAN.role("core")
    (tmp_path / core.file).write_bytes(b"corrupt")
    for pkg in PLAN.packages:
        if pkg.name != "kernel-core":
            (tmp_path / pkg.file).write_bytes(b"good:cached")
    net = FakeNet()
    fetch_all(PLAN, tmp_path, download=net.download, verify=verify_good, sleep=lambda _: None)
    assert net.calls == [core.url]
    assert (tmp_path / core.file).read_bytes() == b"good:" + core.url.encode()


def test_transient_failures_are_retried(tmp_path: Path) -> None:
    net = FakeNet(failures=2)
    fetch_all(PLAN, tmp_path, download=net.download, verify=verify_good, sleep=lambda _: None)
    assert net.calls[:3] == [PLAN.packages[0].url] * 3


def test_persistent_failure_reports_url(tmp_path: Path) -> None:
    net = FakeNet(failures=100)
    with pytest.raises(BuildError, match=r"cannot download .*kernel-5\.14\.0.*connection reset"):
        fetch_all(PLAN, tmp_path, download=net.download, verify=verify_good, sleep=lambda _: None)
    assert not list(tmp_path.glob("*.part"))
    assert not list(tmp_path.glob("*.rpm"))


def test_fetch_file_with_sha256_verifier(tmp_path: Path) -> None:
    good = b"payload"
    digest = __import__("hashlib").sha256(good).hexdigest()

    def download(url: str, dest: Path) -> None:
        dest.write_bytes(good if url.endswith("good") else b"evil")

    out = fetch_file("https://x/good", tmp_path / "f.rpm", sha256_verifier(digest),
                     download=download, sleep=lambda _: None)  # fmt: skip
    assert out.read_bytes() == good
    with pytest.raises(BuildError, match="sha256"):
        fetch_file("https://x/bad", tmp_path / "g.rpm", sha256_verifier(digest),
                   download=download, sleep=lambda _: None)  # fmt: skip
