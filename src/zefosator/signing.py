"""Host side of module/EFI signing input: where the leaf key and certificate come from."""

import hashlib
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from zefosator.config import SigningInfo
from zefosator.errors import ConfigError

KEY_ENV = "ZEFOSA_SIGNING_KEY"
CERT_ENV = "ZEFOSA_SIGNING_CERT"


@dataclass(frozen=True)
class SigningInput:
    cert: Path
    key_file: Path | None = None
    key_pem: str | None = field(default=None, repr=False)  # never printed


def resolve_signing(
    *, key: Path | None, cert: Path | None, unsigned: bool, env: Mapping[str, str]
) -> SigningInput | None:
    key_pem = env.get(KEY_ENV)
    cert = cert or (Path(env[CERT_ENV]) if env.get(CERT_ENV) else None)
    if unsigned:
        if key is not None or cert is not None:
            msg = "--unsigned cannot be combined with --signing-key/--signing-cert"
            raise ConfigError(msg)
        return None
    if cert is None or (key is None and not key_pem):
        msg = (
            "signing input missing: pass --signing-key and --signing-cert (or set "
            f"{KEY_ENV} to the key PEM and {CERT_ENV} to the certificate path), "
            "or --unsigned for a development build"
        )
        raise ConfigError(msg)
    if not cert.is_file():
        msg = f"signing certificate {cert} not found"
        raise ConfigError(msg)
    if key is not None:
        if not key.is_file():
            msg = f"signing key {key} not found"
            raise ConfigError(msg)
        return SigningInput(cert=cert, key_file=key)
    return SigningInput(cert=cert, key_pem=key_pem)


def _openssl(argv: list[str], stdin: bytes | None = None) -> bytes:
    result = subprocess.run(["openssl", *argv], input=stdin, capture_output=True, check=False)
    if result.returncode != 0:
        msg = f"openssl {argv[0]} failed: {result.stderr.decode(errors='replace').strip()[-300:]}"
        raise ConfigError(msg)
    return result.stdout


def describe_cert(cert: Path) -> SigningInfo:
    der = _openssl(["x509", "-in", str(cert), "-outform", "DER"])
    subject = _openssl(["x509", "-in", str(cert), "-noout", "-subject", "-nameopt", "RFC2253"])
    return SigningInfo(
        cert_sha256=hashlib.sha256(der).hexdigest(),
        cert_subject=subject.decode().strip().removeprefix("subject="),
    )


def check_key_matches(signing: SigningInput) -> None:
    cert_key = _openssl(["x509", "-in", str(signing.cert), "-noout", "-pubkey"])
    if signing.key_file is not None:
        key_pub = _openssl(["pkey", "-in", str(signing.key_file), "-pubout"])
    else:
        key_pub = _openssl(["pkey", "-pubout"], stdin=(signing.key_pem or "").encode())
    if key_pub != cert_key:
        msg = f"signing key does not match certificate {signing.cert}"
        raise ConfigError(msg)
