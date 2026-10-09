import struct
import subprocess
from pathlib import Path

import pytest

from zefosator.errors import BuildError
from zefosator.keys import KeyPair
from zefosator.worker.modsign import MODULE_SIG_MAGIC, sign_module, strip_module_signature

Keys = tuple[KeyPair, KeyPair]


def split(signed: bytes) -> tuple[bytes, bytes, tuple[int, ...]]:
    assert signed.endswith(MODULE_SIG_MAGIC)
    info = struct.unpack(">BBBBB3xI", signed[-len(MODULE_SIG_MAGIC) - 12 : -len(MODULE_SIG_MAGIC)])
    sig_len = info[5]
    end = len(signed) - len(MODULE_SIG_MAGIC) - 12
    return signed[: end - sig_len], signed[end - sig_len : end], info


def test_trailer_is_pkcs7_module_signature(module_elf: bytes, signing_keys: Keys) -> None:
    _, leaf = signing_keys
    body, sig, info = split(sign_module(module_elf, leaf.key, leaf.cert_pem))
    assert body == module_elf
    assert info[:5] == (0, 0, 2, 0, 0)
    assert sig[:1] == b"\x30"


def test_signature_embeds_leaf_and_verifies_against_ca(
    tmp_path: Path, module_elf: bytes, signing_keys: Keys
) -> None:
    ca, leaf = signing_keys
    body, sig, _ = split(sign_module(module_elf, leaf.key, leaf.cert_pem))
    (tmp_path / "body").write_bytes(body)
    (tmp_path / "sig").write_bytes(sig)
    sig_path = str(tmp_path / "sig")
    certs = subprocess.run(
        ["openssl", "pkcs7", "-inform", "DER", "-in", sig_path, "-print_certs", "-noout"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "subject=CN=Zefosa TEST leaf" in certs
    verify = subprocess.run(
        [
            "openssl", "cms", "-verify", "-binary", "-inform", "DER", "-in", str(tmp_path / "sig"),
            "-content", str(tmp_path / "body"), "-CAfile", str(ca.cert_pem), "-purpose", "any",
            "-out", "/dev/null",
        ],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert verify.returncode == 0, verify.stderr


def test_modinfo_reports_signer(tmp_path: Path, module_elf: bytes, signing_keys: Keys) -> None:
    _, leaf = signing_keys
    path = tmp_path / "signed.ko"
    path.write_bytes(sign_module(module_elf, leaf.key, leaf.cert_pem))
    info = subprocess.run(
        ["modinfo", "-F", "signer", str(path)], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert info == "Zefosa TEST CA"
    algo = subprocess.run(
        ["modinfo", "-F", "sig_hashalgo", str(path)], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert algo == "sha512"


def test_resigning_replaces_existing_signature(module_elf: bytes, signing_keys: Keys) -> None:
    _, leaf = signing_keys
    once = sign_module(module_elf, leaf.key, leaf.cert_pem)
    twice = sign_module(once, leaf.key, leaf.cert_pem)
    assert split(twice)[0] == module_elf
    assert strip_module_signature(twice) == module_elf
    assert strip_module_signature(module_elf) == module_elf


def test_missing_key_is_a_build_error(
    tmp_path: Path, module_elf: bytes, signing_keys: Keys
) -> None:
    _, leaf = signing_keys
    with pytest.raises(BuildError, match="openssl cms"):
        sign_module(module_elf, tmp_path / "missing.key", leaf.cert_pem)
