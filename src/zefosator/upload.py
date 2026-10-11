"""`upload`: publish packed archives to a WebDAV share, skipping those published unchanged.

The published <name>.sha256 decides whether an archive changed, so the share must allow reading
(listing is not needed). The tar goes first and its .sha256 last: a published checksum always
describes a complete tar, and an interrupted upload is redone by the next run.
"""

import base64
import getpass
import ipaddress
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from zefosator.config import Plan
from zefosator.errors import BuildError, ConfigError
from zefosator.pack import select_archives
from zefosator.worker.manifest import sha256

KEY_ENV = "ZEFOSA_UPLOAD_KEY"
ATTEMPTS = 4  # per request; 5xx and network errors are retried with 1, 2, 4 s pauses
TIMEOUT = 120  # seconds without progress on the connection
_REJECTED = (401, 403)
_NOT_FOUND = 404
_SERVER_ERROR = 500


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """urllib would repeat the Authorization header to wherever a redirect points."""

    def redirect_request(self, *_: object, **__: object) -> None:
        return None


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class WebDavShare:
    """A WebDAV share whose access token is the user name, as Nextcloud public shares use."""

    def __init__(
        self,
        base_url: str,
        key: str,
        *,
        timeout: float = TIMEOUT,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme != "https" and not _loopback(parts.hostname or ""):
            msg = f"{base_url}: refusing to send the upload key over plain http; use https"
            raise ConfigError(msg)
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self._auth = "Basic " + base64.b64encode(f"{key}:".encode()).decode()
        self._timeout = timeout
        self._sleep = sleep
        self._opener = urllib.request.build_opener(_NoRedirects)

    def url(self, name: str) -> str:
        return self.base_url + urllib.parse.quote(name)

    def get(self, name: str) -> bytes | None:
        """The file's contents, or None if it is not published."""
        return self._request("GET", name)

    def put(self, name: str, path: Path) -> None:
        self._request("PUT", name, path)

    def _request(self, method: str, name: str, path: Path | None = None) -> bytes | None:
        url = self.url(name)
        for attempt in range(ATTEMPTS):
            last = attempt == ATTEMPTS - 1
            headers = {"Authorization": self._auth}
            body = None
            try:
                if path is not None:
                    body = path.open("rb")
                    headers["Content-Length"] = str(path.stat().st_size)
                    headers["Content-Type"] = "application/octet-stream"
                # the scheme is checked in __init__: https, or http to this machine
                request = urllib.request.Request(url, data=body, headers=headers, method=method)  # noqa: S310
                with self._opener.open(request, timeout=self._timeout) as response:
                    return bytes(response.read())
            except urllib.error.HTTPError as exc:
                exc.close()
                if exc.code == _NOT_FOUND and method == "GET":
                    return None
                if exc.code in _REJECTED:
                    msg = f"{url}: the server rejected the upload key (HTTP {exc.code})"
                    raise BuildError(msg) from None
                if exc.code < _SERVER_ERROR or last:
                    hint = "; check upload.webdav_url in common.yaml" if exc.code < 400 else ""  # noqa: PLR2004 -- 3xx
                    msg = f"{url}: HTTP {exc.code} {exc.reason}{hint}"
                    raise BuildError(msg) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if last:
                    reason = getattr(exc, "reason", exc)
                    msg = f"{url}: {reason}"
                    raise BuildError(msg) from None
            finally:
                if body is not None:
                    body.close()
            self._sleep(2**attempt)
        msg = f"{url}: no attempts left"  # unreachable: the last attempt returns or raises
        raise BuildError(msg)


@dataclass(frozen=True)
class UploadOutcome:
    name: str
    action: str  # uploaded, unchanged, would upload
    sha256: str
    size: int


@dataclass(frozen=True)
class _Local:
    name: str
    tar: Path
    checksum: Path
    text: bytes
    sha256: str


def _local(directory: Path, name: str) -> _Local:
    tar, checksum = directory / f"{name}.tar", directory / f"{name}.sha256"
    for path in (tar, checksum):
        if not path.is_file():
            msg = f"{path} not found; run `zefosator pack` first"
            raise BuildError(msg)
    digest = sha256(tar)
    text = checksum.read_bytes()
    if text != f"{digest}  {tar.name}\n".encode():
        msg = f"{checksum.name} does not match {tar.name}; run `zefosator pack` again"
        raise BuildError(msg)
    return _Local(name=name, tar=tar, checksum=checksum, text=text, sha256=digest)


def upload(  # noqa: PLR0913 -- two keyword-only options
    plan: Plan,
    root: Path,
    names: Sequence[str],
    share: WebDavShare,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> list[UploadOutcome]:
    """Publish output/<target-id>/archives/<name>.{tar,sha256}; all archives when names is empty."""
    directory = root / "output" / plan.target_id / "archives"
    archives = [_local(directory, a.name) for a in select_archives(plan, names)]
    outcomes = []
    for archive in archives:
        published = share.get(archive.checksum.name)
        size = archive.tar.stat().st_size
        if not force and published is not None and published.strip() == archive.text.strip():
            action = "unchanged"
        elif dry_run:
            action = "would upload"
        else:
            share.put(archive.tar.name, archive.tar)
            share.put(archive.checksum.name, archive.checksum)
            if share.get(archive.checksum.name) != archive.text:
                url = share.url(archive.checksum.name)
                msg = f"{url}: {archive.checksum.name} reads back differently after the upload"
                raise BuildError(msg)
            action = "uploaded"
        outcomes.append(UploadOutcome(archive.name, action, archive.sha256, size))
    return outcomes


def read_upload_key(path: Path, root: Path) -> str:
    """The key from a private file outside the project tree (build containers mount the tree)."""
    if path.resolve().is_relative_to(root.resolve()):
        msg = f"upload key file {path} is inside the project tree {root}; keep keys outside it"
        raise ConfigError(msg)
    try:
        mode = path.stat().st_mode
        key = path.read_text().strip()
    except OSError as exc:
        msg = f"cannot read upload key file {path}: {exc.strerror or exc}"
        raise ConfigError(msg) from exc
    if mode & 0o077:
        msg = f"upload key file {path} is readable by others; chmod 600 {path}"
        raise ConfigError(msg)
    if not key:
        msg = f"upload key file {path} is empty"
        raise ConfigError(msg)
    return key


def resolve_upload_key(key_file: Path | None, root: Path) -> str:
    """--upload-key-file, else $ZEFOSA_UPLOAD_KEY, else a prompt on a terminal."""
    if key_file is not None:
        return read_upload_key(key_file, root)
    key = os.environ.get(KEY_ENV, "").strip()
    if key:
        return key
    if sys.stdin.isatty():
        key = getpass.getpass("Upload key: ").strip()
        if key:
            return key
    msg = f"no upload key: set {KEY_ENV} or pass --upload-key-file"
    raise ConfigError(msg)
