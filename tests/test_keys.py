import stat
import subprocess
from pathlib import Path

import pytest

from zefosator.cli import main
from zefosator.errors import ConfigError
from zefosator.keys import create_ca, create_leaf


def text(cert: Path) -> str:
    return subprocess.run(
        ["openssl", "x509", "-in", str(cert), "-noout", "-text"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_ca_has_only_cert_signing_usage(tmp_path: Path) -> None:
    ca = create_ca(tmp_path / "ca", cn="Test CA", days=30)
    details = text(ca.cert_pem)
    assert "Subject: CN=Test CA" in details
    assert "CA:TRUE" in details
    assert "Certificate Sign, CRL Sign" in details
    assert "Digital Signature" not in details
    assert "Public-Key: (4096 bit)" in details
    assert "sha256WithRSAEncryption" in details
    assert ca.cert_der.read_bytes()[:1] == b"\x30"
    assert len(ca.sha256) == 64
    assert stat.S_IMODE(ca.key.stat().st_mode) == 0o600


def test_leaf_is_code_signing_issued_by_ca(tmp_path: Path) -> None:
    create_ca(tmp_path / "ca", cn="Test CA", days=30)
    leaf = create_leaf(tmp_path / "ca", tmp_path / "leaf", cn="Test signing", days=10)
    details = text(leaf.cert_pem)
    assert "Issuer: CN=Test CA" in details
    assert "CA:FALSE" in details
    assert "Digital Signature" in details
    assert "Code Signing" in details
    assert "X509v3 Authority Key Identifier" in details
    verify = subprocess.run(
        ["openssl", "verify", "-CAfile", str(tmp_path / "ca" / "ca.pem"), str(leaf.cert_pem)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode == 0, verify.stderr
    assert stat.S_IMODE(leaf.key.stat().st_mode) == 0o600


def test_existing_keys_are_never_overwritten(tmp_path: Path) -> None:
    create_ca(tmp_path / "ca", cn="Test CA", days=30)
    with pytest.raises(ConfigError, match="already exists"):
        create_ca(tmp_path / "ca", cn="Other", days=30)


def test_leaf_requires_a_ca(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"ca\.key"):
        create_leaf(tmp_path / "missing", tmp_path / "leaf", cn="x", days=1)


def test_leaf_validity_capped_at_three_years(tmp_path: Path) -> None:
    create_ca(tmp_path / "ca", cn="Test CA", days=30)
    with pytest.raises(ConfigError, match="at most 1095 days"):
        create_leaf(tmp_path / "ca", tmp_path / "leaf", cn="x", days=2000)


def test_cli_keys_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["keys", "ca", "--out", str(tmp_path / "ca"), "--days", "30"]) == 0
    assert (
        main(["keys", "leaf", "--ca", str(tmp_path / "ca"), "--out", str(tmp_path / "leaf")]) == 0
    )
    out = capsys.readouterr().out
    assert "zefosa Secure Boot CA" in out
    assert "SHA-256" in out
    assert (tmp_path / "leaf" / "signing.pem").is_file()
