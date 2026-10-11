"""`pack`: tar + sha256 archives of built RPMs, as listed under `archives:` in the target file."""

import json
import tarfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from zefosator.config import ArchivePlan, Plan
from zefosator.errors import BuildError, ConfigError
from zefosator.worker.manifest import sha256


@dataclass(frozen=True)
class PackedArchive:
    tar: Path
    checksum: Path
    sha256: str
    size: int


def _select(plan: Plan, names: Sequence[str]) -> list[ArchivePlan]:
    defined = {archive.name: archive for archive in plan.archives}
    if not defined:
        msg = f"{plan.target_id}: the target file defines no archives"
        raise ConfigError(msg)
    for name in names:
        if name not in defined:
            msg = f"unknown archive {name!r}; defined: {', '.join(defined)}"
            raise ConfigError(msg)
    return [defined[name] for name in dict.fromkeys(names)] if names else list(plan.archives)


class _Manifests:
    """Each built file must be recorded, with its sha256, by the nearest manifest.json above it."""

    def __init__(self, output: Path) -> None:
        self.output = output
        self._recorded: dict[Path, dict[str, str]] = {}

    def _load(self, manifest: Path) -> dict[str, str]:
        if manifest not in self._recorded:
            try:
                outputs = json.loads(manifest.read_text()).get("outputs", [])
                self._recorded[manifest] = {o["path"]: o["sha256"] for o in outputs}
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                msg = f"cannot read {manifest}: {exc}"
                raise BuildError(msg) from exc
        return self._recorded[manifest]

    def problem(self, rel: str) -> str | None:
        path = self.output / rel
        if not path.is_file():
            return "not found"
        for directory in path.parents:
            manifest = directory / "manifest.json"
            if manifest.is_file():
                shown = f"output/{manifest.relative_to(self.output).as_posix()}"
                recorded = self._load(manifest).get(path.relative_to(directory).as_posix())
                if recorded is None:
                    return f"not listed in {shown}"
                if recorded != sha256(path):
                    return f"sha256 differs from {shown}"
                return None
            if directory == self.output:
                break
        return "no manifest.json records it"


def _add(tar: tarfile.TarFile, path: Path) -> None:
    """Flat, owner-neutral members: the same files always give the same archive."""
    info = tarfile.TarInfo(path.name)
    stat = path.stat()
    info.size = stat.st_size
    info.mtime = int(stat.st_mtime)
    info.mode = 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    with path.open("rb") as handle:
        tar.addfile(info, handle)


def _write(archive: ArchivePlan, output: Path, dest: Path) -> PackedArchive:
    tar_path, checksum = dest / f"{archive.name}.tar", dest / f"{archive.name}.sha256"
    tmp_tar, tmp_sum = dest / f".{tar_path.name}.tmp", dest / f".{checksum.name}.tmp"
    try:
        with tarfile.open(tmp_tar, "w", format=tarfile.PAX_FORMAT) as tar:
            for rel in archive.files:
                _add(tar, output / rel)
        digest = sha256(tmp_tar)
        tmp_sum.write_text(f"{digest}  {tar_path.name}\n")
        # never leave a checksum next to a tar it does not describe
        checksum.unlink(missing_ok=True)
        tmp_tar.replace(tar_path)
        tmp_sum.replace(checksum)
    finally:
        tmp_tar.unlink(missing_ok=True)
        tmp_sum.unlink(missing_ok=True)
    return PackedArchive(
        tar=tar_path, checksum=checksum, sha256=digest, size=tar_path.stat().st_size
    )


def pack(plan: Plan, root: Path, names: Sequence[str]) -> list[PackedArchive]:
    """Write output/<target-id>/archives/<name>.{tar,sha256}; all archives when names is empty."""
    archives = _select(plan, names)
    output = root / "output"
    manifests = _Manifests(output)
    files = dict.fromkeys(rel for archive in archives for rel in archive.files)
    problems = [f"{rel}: {problem}" for rel in files if (problem := manifests.problem(rel))]
    if problems:
        msg = "cannot pack: these files do not match the build outputs\n  " + "\n  ".join(problems)
        raise BuildError(msg)
    dest = output / plan.target_id / "archives"
    dest.mkdir(parents=True, exist_ok=True)
    return [_write(archive, output, dest) for archive in archives]
