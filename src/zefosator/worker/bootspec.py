"""Rendering and building the hand-written zefosa boot package specs."""

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, PackageLoader, StrictUndefined

from zefosator.worker.proc import run
from zefosator.worker.rpmheader import Dependency, DepKind, FileEntry
from zefosator.worker.specgen import dependency_line, escape, file_line

_ENV = Environment(
    loader=PackageLoader("zefosator", "templates"),
    undefined=StrictUndefined,
    autoescape=False,  # noqa: S701 -- renders rpm specs, not HTML; escaping is done in Python
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
)
CHANGELOG_AUTHOR = "Zefosa <zefosa@fogas.net>"


@dataclass(frozen=True)
class SpecPackage:
    name: str
    version: str
    release: str
    summary: str
    license: str
    url: str
    description: str
    arch: str  # "noarch" or a machine arch
    deps: Mapping[DepKind, Sequence[Dependency]]
    files: Sequence[FileEntry]
    changelog: str


def render_boot_spec(pkg: SpecPackage, *, payload: str = "w3T.xzdio") -> str:
    deps = [dependency_line(kind, d) for kind in DepKind for d in pkg.deps.get(kind, ())]
    return _ENV.get_template("boot/package.spec.j2").render(
        pkg=pkg,
        summary=escape(pkg.summary),
        license=escape(pkg.license),
        description=escape(pkg.description),
        deps=deps,
        files=[file_line(f) for f in pkg.files],
        changelog_heading=f"{time.strftime('%a %b %d %Y', time.gmtime())} {CHANGELOG_AUTHOR} - "
        f"{pkg.version}-{pkg.release}",
        changelog=escape(pkg.changelog),
        payload=payload,
    )


@dataclass(frozen=True)
class BuildDirs:
    root: Path  # staged buildroot content
    workdir: Path
    outdir: Path
    log: Path


def rpmbuild(spec_text: str, dirs: BuildDirs, arch: str) -> None:
    spec = dirs.workdir / "package.spec"
    spec.write_text(spec_text)
    dirs.outdir.mkdir(parents=True, exist_ok=True)
    run(
        [
            "rpmbuild", "-bb", "--target", arch,
            "--define", f"_topdir {dirs.workdir / 'rpmbuild'}",
            "--define", f"_rpmdir {dirs.outdir}",
            "--define", "_build_name_fmt %%{NAME}-%%{VERSION}-%%{RELEASE}.%%{ARCH}.rpm",
            "--define", f"_zefosa_root {dirs.root}",
            str(spec),
        ],
        log=dirs.log,
    )  # fmt: skip
