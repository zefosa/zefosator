"""Download upstream kernel RPMs into the cache and verify them with `rpm -K`."""

import hashlib
import shutil
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

from zefosator.config import Plan
from zefosator.errors import BuildError
from zefosator.worker.proc import run

ATTEMPTS = 3
RETRY_DELAY = 5.0
TIMEOUT = 120.0

Downloader = Callable[[str, Path], None]
Verifier = Callable[[Path], None]


def http_download(url: str, dest: Path) -> None:
    if not url.startswith("https://"):
        msg = f"refusing non-https URL {url}"
        raise BuildError(msg)
    with urllib.request.urlopen(url, timeout=TIMEOUT) as response, dest.open("wb") as out:  # noqa: S310 -- https enforced above
        shutil.copyfileobj(response, out)


def rpm_verifier(*, require_signature: bool) -> Verifier:
    def verify(path: Path) -> None:
        output = run(["rpm", "-K", str(path)])
        if require_signature and "signatures OK" not in output:
            msg = f"{path.name}: no valid vendor signature: {output.strip()}"
            raise BuildError(msg)

    return verify


def import_keys(keys: tuple[str, ...]) -> None:
    for key in keys:
        run(["rpm", "--import", key])


def _download_verified(
    url: str, dest: Path, download: Downloader, verify: Verifier, sleep: Callable[[float], None]
) -> None:
    part = dest.with_name(dest.name + ".part")
    error: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            download(url, part)
            verify(part)
        except (OSError, BuildError) as exc:
            error = exc
            part.unlink(missing_ok=True)
            if attempt < ATTEMPTS:
                sleep(RETRY_DELAY * attempt)
            continue
        part.rename(dest)
        return
    msg = f"cannot download {url} after {ATTEMPTS} attempts: {error}"
    raise BuildError(msg)


def sha256_verifier(expected: str) -> Verifier:
    def verify(path: Path) -> None:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected:
            msg = f"{path.name}: sha256 {digest} does not match the pinned {expected}"
            raise BuildError(msg)

    return verify


def fetch_file(
    url: str,
    dest: Path,
    verify: Verifier,
    *,
    download: Downloader = http_download,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    """Keep a verified cached `dest`, otherwise download it (with retries) and verify it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file():
        try:
            verify(dest)
        except BuildError:
            dest.unlink()
        else:
            return dest
    _download_verified(url, dest, download, verify, sleep)
    return dest


def fetch_all(
    plan: Plan,
    downloads: Path,
    *,
    download: Downloader = http_download,
    verify: Verifier | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Path]:
    """Return package name -> verified local RPM, downloading what is missing or invalid."""
    if verify is None:
        import_keys(plan.gpg_keys)
        verify = rpm_verifier(require_signature=bool(plan.gpg_keys))
    paths: dict[str, Path] = {}
    for pkg in plan.packages:
        paths[pkg.name] = fetch_file(
            pkg.url, downloads / pkg.file, verify, download=download, sleep=sleep
        )
    return paths
