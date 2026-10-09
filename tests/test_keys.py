import stat
import subprocess
from pathlib import Path

import pytest

from zefosator.cli import main
from zefosator.errors import ConfigError
from zefosator.keys import ca_key_encrypted, create_ca, create_leaf


def text(cert: Path) -> str:
    return subprocess.run(
        ["openssl", "x509", "-in", str(cert), "-noout", "-text"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_ca_has_only_cert_signing_usage(tmp_path: Path) -> None:
    ca = create_ca(tmp_path / "ca", passphrase=None, cn="Test CA", days=30)
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
    create_ca(tmp_path / "ca", passphrase=None, cn="Test CA", days=30)
    leaf = create_leaf(
        tmp_path / "ca", tmp_path / "leaf", ca_passphrase=None, cn="Test signing", days=10
    )
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
    create_ca(tmp_path / "ca", passphrase=None, cn="Test CA", days=30)
    with pytest.raises(ConfigError, match="already exists"):
        create_ca(tmp_path / "ca", passphrase=None, cn="Other", days=30)


def test_leaf_requires_a_ca(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"ca\.key"):
        create_leaf(tmp_path / "missing", tmp_path / "leaf", ca_passphrase=None, cn="x", days=1)


def test_leaf_validity_capped_at_three_years(tmp_path: Path) -> None:
    create_ca(tmp_path / "ca", passphrase=None, cn="Test CA", days=30)
    with pytest.raises(ConfigError, match="at most 1095 days"):
        create_leaf(tmp_path / "ca", tmp_path / "leaf", ca_passphrase=None, cn="x", days=2000)


PASSPHRASE = "correct horse battery staple"


def openssl(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["openssl", *args], input=stdin, capture_output=True, text=True, check=False
    )


def test_ca_key_is_written_only_encrypted(tmp_path: Path) -> None:
    ca = create_ca(tmp_path / "ca", passphrase=PASSPHRASE, cn="Test CA", days=30)
    pem = ca.key.read_text()
    assert pem.startswith("-----BEGIN ENCRYPTED PRIVATE KEY-----")
    assert stat.S_IMODE(ca.key.stat().st_mode) == 0o600
    asn1 = openssl("asn1parse", "-in", str(ca.key)).stdout
    assert ":PBKDF2" in asn1
    assert ":hmacWithSHA256" in asn1
    assert ":aes-256-cbc" in asn1
    assert "INTEGER           :0927C0" in asn1  # 600000 PBKDF2 iterations
    assert openssl("pkey", "-in", str(ca.key), "-noout", "-passin", "pass:wrong").returncode != 0
    right = openssl("pkey", "-in", str(ca.key), "-noout", "-passin", "stdin", stdin=PASSPHRASE)
    assert right.returncode == 0, right.stderr
    assert ca_key_encrypted(tmp_path / "ca")


def test_unencrypted_ca_is_reported_as_such(tmp_path: Path) -> None:
    create_ca(tmp_path / "ca", passphrase=None, cn="Test CA", days=30)
    assert not ca_key_encrypted(tmp_path / "ca")


def test_ca_passphrase_must_be_long_enough(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="at least 12 characters"):
        create_ca(tmp_path / "ca", passphrase="short", cn="Test CA", days=30)
    assert not (tmp_path / "ca").exists()


def test_leaf_from_encrypted_ca_chains_to_it(tmp_path: Path) -> None:
    create_ca(tmp_path / "ca", passphrase=PASSPHRASE, cn="Test CA", days=30)
    leaf = create_leaf(
        tmp_path / "ca", tmp_path / "leaf", ca_passphrase=PASSPHRASE, cn="Test signing", days=10
    )
    verify = openssl("verify", "-CAfile", str(tmp_path / "ca" / "ca.pem"), str(leaf.cert_pem))
    assert verify.returncode == 0, verify.stderr
    # the leaf key is used unattended by builds, so it stays unencrypted
    assert leaf.key.read_text().startswith("-----BEGIN PRIVATE KEY-----")


def test_leaf_refuses_a_wrong_ca_passphrase_and_leaves_no_files(tmp_path: Path) -> None:
    create_ca(tmp_path / "ca", passphrase=PASSPHRASE, cn="Test CA", days=30)
    with pytest.raises(ConfigError, match="wrong passphrase"):
        create_leaf(tmp_path / "ca", tmp_path / "leaf", ca_passphrase="not the passphrase", days=10)
    assert not (tmp_path / "leaf").exists()


def test_leaf_needs_the_passphrase_of_an_encrypted_ca(tmp_path: Path) -> None:
    create_ca(tmp_path / "ca", passphrase=PASSPHRASE, cn="Test CA", days=30)
    with pytest.raises(ConfigError, match="encrypted"):
        create_leaf(tmp_path / "ca", tmp_path / "leaf", ca_passphrase=None, days=10)
    assert not (tmp_path / "leaf").exists()


def prompts(monkeypatch: pytest.MonkeyPatch, *answers: str) -> list[str]:
    asked: list[str] = []
    replies = iter(answers)

    def fake_getpass(prompt: str = "") -> str:
        asked.append(prompt)
        return next(replies)

    monkeypatch.setattr("getpass.getpass", fake_getpass)
    return asked


def test_cli_keys_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    asked = prompts(monkeypatch, PASSPHRASE, PASSPHRASE, PASSPHRASE)
    assert main(["keys", "ca", "--out", str(tmp_path / "ca"), "--days", "30"]) == 0
    assert (
        main(["keys", "leaf", "--ca", str(tmp_path / "ca"), "--out", str(tmp_path / "leaf")]) == 0
    )
    assert len(asked) == 3
    assert "repeat" in asked[1].lower()
    assert str(tmp_path / "ca" / "ca.key") in asked[2]
    out = capsys.readouterr().out
    assert "zefosa Secure Boot CA" in out
    assert "SHA-256" in out
    assert ca_key_encrypted(tmp_path / "ca")
    assert (tmp_path / "leaf" / "signing.pem").is_file()


def test_cli_ca_rejects_mismatched_passphrases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prompts(monkeypatch, PASSPHRASE, PASSPHRASE + "x")
    assert main(["keys", "ca", "--out", str(tmp_path / "ca")]) == 1
    assert "do not match" in capsys.readouterr().err
    assert not (tmp_path / "ca").exists()


def test_cli_leaf_does_not_prompt_for_an_unencrypted_ca(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_ca(tmp_path / "ca", passphrase=None, cn="Test CA", days=30)
    asked = prompts(monkeypatch)
    assert (
        main(["keys", "leaf", "--ca", str(tmp_path / "ca"), "--out", str(tmp_path / "leaf")]) == 0
    )
    assert asked == []
