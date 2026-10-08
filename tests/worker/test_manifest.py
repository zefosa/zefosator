import hashlib
import json
from pathlib import Path

from zefosator.config import load_plan
from zefosator.worker.manifest import write_manifest

ROOT = Path(__file__).resolve().parents[2]
PLAN = load_plan(ROOT / "targets" / "fedora-44-x86_64.yaml")


def test_manifest_lists_inputs_outputs_and_zfs(tmp_path: Path) -> None:
    core = tmp_path / "in" / PLAN.role("core").file
    core.parent.mkdir()
    core.write_bytes(b"core")
    out = tmp_path / "out" / "kernel" / "kernel-core-7.2.9-200.zefosa.fc44.x86_64.rpm"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"rebuilt")
    manifest = tmp_path / "out" / "manifest.json"
    write_manifest(
        manifest,
        PLAN,
        {"kernel-core": core},
        "0123abcd",
        [out],
        nevra=lambda p: f"nevra-of-{p.name}",
    )
    data = json.loads(manifest.read_text())
    assert data["plan"]["zefosa_release"] == "200.zefosa.fc44"
    assert data["zfs"] == {
        "tag": "zfs-2.4.4",
        "commit": "0123abcd",
        "cherry_picks": [],
        "patches": [],
    }
    assert data["inputs"] == [
        {
            "name": "kernel-core",
            "url": PLAN.role("core").url,
            "file": core.name,
            "sha256": hashlib.sha256(b"core").hexdigest(),
        }
    ]
    assert data["outputs"] == [
        {
            "path": "kernel/kernel-core-7.2.9-200.zefosa.fc44.x86_64.rpm",
            "nevra": "nevra-of-kernel-core-7.2.9-200.zefosa.fc44.x86_64.rpm",
            "sha256": hashlib.sha256(b"rebuilt").hexdigest(),
        }
    ]
