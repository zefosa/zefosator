from pathlib import Path

import pytest

from zefosator.keys import KeyPair, create_ca, create_leaf


@pytest.fixture(scope="session")
def signing_keys(tmp_path_factory: pytest.TempPathFactory) -> tuple[KeyPair, KeyPair]:
    """A throwaway test CA and leaf (RSA 4096 generation is slow, so share them)."""
    base = tmp_path_factory.mktemp("keys")
    ca = create_ca(base / "ca", cn="Zefosa TEST CA", days=30)
    leaf = create_leaf(base / "ca", base / "leaf", cn="Zefosa TEST leaf", days=30)
    return ca, leaf


@pytest.fixture
def module_elf(tmp_path: Path) -> bytes:
    """A minimal ELF relocatable object with a .modinfo section, enough for modinfo(8)."""
    import shutil  # noqa: PLC0415 -- only needed by this fixture
    import subprocess  # noqa: PLC0415 -- only needed by this fixture

    if shutil.which("objcopy") is None:
        pytest.skip("objcopy (binutils) is required")
    info = tmp_path / "modinfo.bin"
    info.write_bytes(b"license=GPL\x00name=zfstest\x00")
    out = tmp_path / "zfstest.ko"
    subprocess.run(
        [
            "objcopy", "-I", "binary", "-O", "elf64-x86-64", "-B", "i386:x86-64",
            "--rename-section", ".data=.modinfo", str(info), str(out),
        ],
        check=True,
    )  # fmt: skip
    return out.read_bytes()
