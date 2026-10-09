from pathlib import Path

import pytest

from zefosator.errors import ConfigError
from zefosator.keys import KeyPair
from zefosator.signing import (
    SigningInput,
    check_key_location,
    check_key_matches,
    describe_cert,
    resolve_signing,
)

Keys = tuple[KeyPair, KeyPair]


def test_unsigned_needs_no_key() -> None:
    assert resolve_signing(key=None, cert=None, unsigned=True, env={}) is None


def test_missing_signing_input_is_an_error() -> None:
    with pytest.raises(ConfigError, match=r"--signing-key.*--unsigned"):
        resolve_signing(key=None, cert=None, unsigned=False, env={})


def test_unsigned_conflicts_with_keys(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="--unsigned"):
        resolve_signing(key=tmp_path / "k", cert=tmp_path / "c", unsigned=True, env={})


def test_key_file_and_cert_flags(signing_keys: Keys) -> None:
    _, leaf = signing_keys
    signing = resolve_signing(key=leaf.key, cert=leaf.cert_pem, unsigned=False, env={})
    assert signing == SigningInput(cert=leaf.cert_pem, key_file=leaf.key)


def test_key_text_and_cert_from_environment(signing_keys: Keys) -> None:
    _, leaf = signing_keys
    pem = leaf.key.read_text()
    env = {"ZEFOSA_SIGNING_KEY": pem, "ZEFOSA_SIGNING_CERT": str(leaf.cert_pem)}
    signing = resolve_signing(key=None, cert=None, unsigned=False, env=env)
    assert signing is not None
    assert signing.key_pem == pem
    assert signing.cert == leaf.cert_pem
    assert "PRIVATE KEY" not in repr(signing)


def test_missing_cert_file_is_an_error(signing_keys: Keys, tmp_path: Path) -> None:
    _, leaf = signing_keys
    with pytest.raises(ConfigError, match="certificate"):
        resolve_signing(key=leaf.key, cert=tmp_path / "nope.pem", unsigned=False, env={})


def test_describe_cert(signing_keys: Keys) -> None:
    _, leaf = signing_keys
    info = describe_cert(leaf.cert_pem)
    assert info.cert_sha256 == leaf.sha256
    assert info.cert_subject == "CN=Zefosa TEST leaf"


def test_key_must_match_cert(signing_keys: Keys) -> None:
    ca, leaf = signing_keys
    check_key_matches(SigningInput(cert=leaf.cert_pem, key_file=leaf.key))
    check_key_matches(SigningInput(cert=leaf.cert_pem, key_pem=leaf.key.read_text()))
    with pytest.raises(ConfigError, match="does not match"):
        check_key_matches(SigningInput(cert=leaf.cert_pem, key_file=ca.key))


def test_relative_paths_become_absolute(
    signing_keys: Keys, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, leaf = signing_keys
    monkeypatch.chdir(leaf.key.parent)
    signing = resolve_signing(
        key=Path(leaf.key.name), cert=Path(leaf.cert_pem.name), unsigned=False, env={}
    )
    assert signing is not None
    assert signing.cert.is_absolute()
    assert signing.key_file is not None
    assert signing.key_file.is_absolute()


def test_key_inside_project_tree_is_rejected(tmp_path: Path) -> None:
    key = tmp_path / "project" / "cache" / "signing.key"
    key.parent.mkdir(parents=True)
    key.write_text("k")
    with pytest.raises(ConfigError, match="outside the project tree"):
        check_key_location(SigningInput(cert=key, key_file=key), tmp_path / "project")
    check_key_location(SigningInput(cert=key, key_pem="pem"), tmp_path / "project")
