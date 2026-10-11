import base64
import hashlib
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.test_config import COMMON_YAML, MINI_TARGET, make_root
from tests.test_pack import ARCHIVES, TID, write_outputs
from zefosator.cli import main
from zefosator.config import load_plan
from zefosator.errors import BuildError, ConfigError
from zefosator.pack import pack
from zefosator.upload import WebDavShare, read_upload_key, upload

KEY = "s3cr3t-share-token"


class Share:
    """A WebDAV share that reads and writes files but cannot list them."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.requests: list[tuple[str, str]] = []
        self.failures: list[int] = []  # status codes for the next requests
        self.answer_after_put: bytes | None = None  # a server that stores something else


class Handler(BaseHTTPRequestHandler):
    server: "ShareServer"

    def _check(self) -> str | None:
        share = self.server.share
        name = self.path.removeprefix("/dav/")
        share.requests.append((self.command, name))
        if share.failures:
            self.send_error(share.failures.pop(0))
            return None
        expected = "Basic " + base64.b64encode(f"{KEY}:".encode()).decode()
        if self.headers.get("Authorization") != expected:
            self.send_error(401)
            return None
        return name

    def do_GET(self) -> None:
        name = self._check()
        if name is None:
            return
        data = self.server.share.files.get(name)
        if data is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_PUT(self) -> None:
        name = self._check()
        if name is None:
            return
        data = self.rfile.read(int(self.headers["Content-Length"]))
        share = self.server.share
        share.files[name] = data
        if name.endswith(".sha256") and share.answer_after_put is not None:
            share.files[name] = share.answer_after_put
        self.send_response(201)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


class ShareServer(ThreadingHTTPServer):
    share: Share


@pytest.fixture
def server() -> Iterator[ShareServer]:
    httpd = ShareServer(("127.0.0.1", 0), Handler)
    httpd.share = Share()
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def url(server: ShareServer) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}/dav/"


@pytest.fixture
def target(tmp_path: Path, server: ShareServer) -> Path:
    path = make_root(
        tmp_path, target=MINI_TARGET.format(os_version="9.8", arch="x86_64") + ARCHIVES
    )
    (tmp_path / "common.yaml").write_text(COMMON_YAML + f"upload:\n  webdav_url: {url(server)}\n")
    output = tmp_path / "output"
    write_outputs(output, TID, {"zfs/zfs-2.3.9.rpm": b"zfs", "kernel/kernel-5.14.0.rpm": b"kern"})
    write_outputs(output, f"{TID}/boot", {"shim.rpm": b"shim"})
    write_outputs(output, "boot", {"zefosa-boot.rpm": b"boot"})
    pack(load_plan(target_file := path), tmp_path, [])
    return target_file


def archives(tmp_path: Path) -> Path:
    return tmp_path / "output" / TID / "archives"


def share(server: ShareServer, key: str = KEY) -> WebDavShare:
    return WebDavShare(url(server), key, sleep=lambda _: None)


def test_upload_sends_the_tar_then_its_checksum(
    tmp_path: Path, target: Path, server: ShareServer
) -> None:
    outcomes = upload(load_plan(target), tmp_path, ["mini-kernel"], share(server))
    assert [(o.name, o.action) for o in outcomes] == [("mini-kernel", "uploaded")]
    puts = [name for method, name in server.share.requests if method == "PUT"]
    assert puts == ["mini-kernel.tar", "mini-kernel.sha256"]
    local = archives(tmp_path)
    assert server.share.files["mini-kernel.tar"] == (local / "mini-kernel.tar").read_bytes()
    assert server.share.files["mini-kernel.sha256"] == (local / "mini-kernel.sha256").read_bytes()
    assert (
        outcomes[0].sha256 == hashlib.sha256((local / "mini-kernel.tar").read_bytes()).hexdigest()
    )


def test_archive_published_unchanged_is_skipped(
    tmp_path: Path, target: Path, server: ShareServer
) -> None:
    sums = archives(tmp_path) / "mini-kernel.sha256"
    server.share.files["mini-kernel.sha256"] = sums.read_bytes()
    outcomes = upload(load_plan(target), tmp_path, [], share(server))
    assert [(o.name, o.action) for o in outcomes] == [
        ("mini-zfs", "uploaded"),
        ("mini-kernel", "unchanged"),
    ]
    assert ("PUT", "mini-kernel.tar") not in server.share.requests


def test_archive_with_another_published_checksum_is_uploaded(
    tmp_path: Path, target: Path, server: ShareServer
) -> None:
    server.share.files["mini-kernel.sha256"] = b"0" * 64 + b"  mini-kernel.tar\n"
    (outcome,) = upload(load_plan(target), tmp_path, ["mini-kernel"], share(server))
    assert outcome.action == "uploaded"


def test_force_uploads_an_unchanged_archive(
    tmp_path: Path, target: Path, server: ShareServer
) -> None:
    sums = archives(tmp_path) / "mini-kernel.sha256"
    server.share.files["mini-kernel.sha256"] = sums.read_bytes()
    (outcome,) = upload(load_plan(target), tmp_path, ["mini-kernel"], share(server), force=True)
    assert outcome.action == "uploaded"
    assert ("PUT", "mini-kernel.tar") in server.share.requests


def test_dry_run_only_reads(tmp_path: Path, target: Path, server: ShareServer) -> None:
    sums = archives(tmp_path) / "mini-kernel.sha256"
    server.share.files["mini-kernel.sha256"] = sums.read_bytes()
    outcomes = upload(load_plan(target), tmp_path, [], share(server), dry_run=True)
    assert [(o.name, o.action) for o in outcomes] == [
        ("mini-zfs", "would upload"),
        ("mini-kernel", "unchanged"),
    ]
    assert {method for method, _ in server.share.requests} == {"GET"}


def test_transient_server_errors_are_retried(
    tmp_path: Path, target: Path, server: ShareServer
) -> None:
    server.share.failures = [503, 502]
    (outcome,) = upload(load_plan(target), tmp_path, ["mini-kernel"], share(server))
    assert outcome.action == "uploaded"


def test_persistent_server_errors_give_up(
    tmp_path: Path, target: Path, server: ShareServer
) -> None:
    server.share.failures = [503] * 10
    with pytest.raises(BuildError, match=r"/dav/mini-kernel\.sha256: HTTP 503") as error:
        upload(load_plan(target), tmp_path, ["mini-kernel"], share(server))
    assert KEY not in str(error.value)


def test_a_rejected_key_fails_without_retrying(
    tmp_path: Path, target: Path, server: ShareServer
) -> None:
    with pytest.raises(BuildError, match=r"rejected the upload key \(HTTP 401\)") as error:
        upload(load_plan(target), tmp_path, ["mini-kernel"], share(server, key="wrong-key"))
    assert "wrong-key" not in str(error.value)
    assert len(server.share.requests) == 1


def test_a_checksum_that_does_not_match_its_tar_is_never_uploaded(
    tmp_path: Path, target: Path, server: ShareServer
) -> None:
    (archives(tmp_path) / "mini-zfs.tar").write_bytes(b"changed after pack")
    with pytest.raises(BuildError, match=r"mini-zfs\.sha256 does not match mini-zfs\.tar.*pack"):
        upload(load_plan(target), tmp_path, [], share(server))
    assert server.share.requests == []


def test_an_archive_that_was_not_packed_is_reported(
    tmp_path: Path, target: Path, server: ShareServer
) -> None:
    (archives(tmp_path) / "mini-zfs.tar").unlink()
    with pytest.raises(BuildError, match=r"mini-zfs\.tar not found.*zefosator pack"):
        upload(load_plan(target), tmp_path, ["mini-zfs"], share(server))
    assert server.share.requests == []


def test_unknown_archive_name_is_rejected(
    tmp_path: Path, target: Path, server: ShareServer
) -> None:
    with pytest.raises(ConfigError, match="unknown archive 'nope'"):
        upload(load_plan(target), tmp_path, ["nope"], share(server))


def test_upload_is_verified_by_reading_the_checksum_back(
    tmp_path: Path, target: Path, server: ShareServer
) -> None:
    server.share.answer_after_put = b"something else\n"
    with pytest.raises(BuildError, match=r"mini-kernel\.sha256 reads back differently"):
        upload(load_plan(target), tmp_path, ["mini-kernel"], share(server))


def test_key_file_must_be_private_and_outside_the_project(tmp_path: Path) -> None:
    root, outside = tmp_path / "project", tmp_path / "secrets"
    root.mkdir()
    outside.mkdir()
    good = outside / "upload.key"
    good.write_text(f"{KEY}\n")
    good.chmod(0o600)
    assert read_upload_key(good, root) == KEY
    good.chmod(0o640)
    with pytest.raises(ConfigError, match="readable by others; chmod 600"):
        read_upload_key(good, root)
    inside = root / "upload.key"
    inside.write_text(KEY)
    inside.chmod(0o600)
    with pytest.raises(ConfigError, match="inside the project tree"):
        read_upload_key(inside, root)
    empty = outside / "empty.key"
    empty.write_text("\n")
    empty.chmod(0o600)
    with pytest.raises(ConfigError, match="is empty"):
        read_upload_key(empty, root)


def test_cli_upload_with_key_from_environment(
    tmp_path: Path,
    target: Path,
    server: ShareServer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ZEFOSA_UPLOAD_KEY", KEY)
    assert main(["upload", str(target), "mini-kernel"]) == 0
    out = capsys.readouterr().out
    assert "mini-kernel: uploaded" in out
    assert KEY not in out
    assert main(["upload", str(target), "mini-kernel"]) == 0
    assert "mini-kernel: unchanged" in capsys.readouterr().out


def test_cli_upload_without_a_key_fails_when_it_cannot_ask(
    target: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ZEFOSA_UPLOAD_KEY", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(["upload", str(target)]) == 1
    assert "ZEFOSA_UPLOAD_KEY" in capsys.readouterr().err


def test_cli_upload_asks_for_the_key_on_a_terminal(
    target: Path, server: ShareServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ZEFOSA_UPLOAD_KEY", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": KEY)
    assert main(["upload", str(target), "mini-kernel", "--dry-run"]) == 0
    assert {method for method, _ in server.share.requests} == {"GET"}


def test_cli_upload_needs_an_upload_section(
    tmp_path: Path, target: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "common.yaml").write_text(COMMON_YAML)
    assert main(["upload", str(target), "--upload-key-file", "/nonexistent"]) == 1
    assert "no upload section" in capsys.readouterr().err


@pytest.mark.parametrize("base", ["http://cloud.example.org/dav/", "http://10.0.0.5/dav/"])
def test_the_key_never_travels_over_plain_http_off_this_machine(base: str) -> None:
    with pytest.raises(ConfigError, match="plain http"):
        WebDavShare(base, KEY)


def test_redirects_are_not_followed(tmp_path: Path, target: Path, server: ShareServer) -> None:
    server.share.failures = [302]
    with pytest.raises(BuildError, match=r"HTTP 302.*upload\.webdav_url"):
        upload(load_plan(target), tmp_path, ["mini-kernel"], share(server))
