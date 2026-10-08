"""manifest.json: what went into a build and what came out."""

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from zefosator.config import Plan
from zefosator.worker.proc import run


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rpm_nevra(path: Path) -> str:
    return run(["rpm", "-qp", "--qf", "%{NEVRA}", str(path)]).strip()


def write_manifest(  # noqa: PLR0913 -- one argument per manifest section
    path: Path,
    plan: Plan,
    inputs: Mapping[str, Path],
    zfs_commit: str,
    outputs: Sequence[Path],
    *,
    nevra: Callable[[Path], str] = rpm_nevra,
) -> None:
    urls = {p.name: p.url for p in plan.packages}
    data = {
        "plan": plan.model_dump(mode="json"),
        "zfs": {
            "tag": plan.zfs.tag,
            "commit": zfs_commit,
            "cherry_picks": list(plan.zfs.cherry_picks),
            "patches": list(plan.zfs.patches),
        },
        "inputs": [
            {"name": name, "url": urls[name], "file": rpm.name, "sha256": sha256(rpm)}
            for name, rpm in inputs.items()
        ],
        "outputs": [
            {"path": str(out.relative_to(path.parent)), "nevra": nevra(out), "sha256": sha256(out)}
            for out in outputs
        ],
    }
    path.write_text(json.dumps(data, indent=2) + "\n")
