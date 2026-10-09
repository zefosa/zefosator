"""Create the zefosa Secure Boot CA and issue signing leaf certificates (spec section 3)."""

import hashlib
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path

from zefosator.errors import BuildError, ConfigError
from zefosator.worker.proc import run

CA_DAYS = 7300
LEAF_MAX_DAYS = 1095
CA_CN = "zefosa Secure Boot CA"
LEAF_CN = "zefosa signing key"
MIN_PASSPHRASE = 12
PBKDF2_ITERATIONS = 600_000
_PASS_ENV = "ZEFOSA_CA_PASSPHRASE"  # noqa: S105 -- the variable name, not a password
_ENCRYPTED_PEM = "-----BEGIN ENCRYPTED PRIVATE KEY-----"
_CA_EXTENSIONS = (
    "basicConstraints=critical,CA:TRUE",
    "keyUsage=critical,keyCertSign,cRLSign",
    "subjectKeyIdentifier=hash",
)
_LEAF_EXTENSIONS = (
    "basicConstraints=critical,CA:FALSE",
    "keyUsage=critical,digitalSignature",
    "extendedKeyUsage=codeSigning",
    "subjectKeyIdentifier=hash",
    "authorityKeyIdentifier=keyid",
)


@dataclass(frozen=True)
class KeyPair:
    key: Path
    cert_pem: Path
    cert_der: Path
    sha256: str


def _prepare(out: Path, names: tuple[str, ...]) -> None:
    existing = [n for n in names if (out / n).exists()]
    if existing:
        msg = f"{out / existing[0]} already exists; refusing to overwrite keys"
        raise ConfigError(msg)
    out.mkdir(parents=True, exist_ok=True)


def _private_file(path: Path) -> None:
    """Create an empty 0600 file so openssl writes the private key into it."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)


def _finish(key: Path, pem: Path, der: Path) -> KeyPair:
    run(["openssl", "x509", "-in", str(pem), "-outform", "DER", "-out", str(der)])
    return KeyPair(
        key=key, cert_pem=pem, cert_der=der, sha256=hashlib.sha256(der.read_bytes()).hexdigest()
    )


def _write_ca_key(key: Path, passphrase: str | None) -> None:
    """Generate the CA key in memory; on disk it only ever exists encrypted (if a passphrase)."""
    pem = run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:4096"])
    _private_file(key)
    if passphrase is None:
        key.write_text(pem)
        return
    run(
        [
            "openssl", "pkcs8", "-topk8", "-v2", "aes-256-cbc", "-v2prf", "hmacWithSHA256",
            "-iter", str(PBKDF2_ITERATIONS), "-passout", f"env:{_PASS_ENV}", "-out", str(key),
        ],
        env={**os.environ, _PASS_ENV: passphrase},
        stdin=pem,
    )  # fmt: skip


def _passin(passphrase: str | None) -> tuple[list[str], str | None]:
    """openssl arguments and stdin that unlock a CA key (none for an unencrypted one)."""
    return (["-passin", "stdin"], passphrase + "\n") if passphrase is not None else ([], None)


def _unlock_check(key: Path, passphrase: str | None, *, encrypted: bool) -> None:
    """Fail before any leaf file is written if the CA key cannot be unlocked."""
    if not encrypted:
        return
    if passphrase is None:
        msg = f"{key} is encrypted; its passphrase is required"
        raise ConfigError(msg)
    passin, stdin = _passin(passphrase)
    try:
        run(["openssl", "pkey", "-in", str(key), *passin, "-noout"], stdin=stdin)
    except BuildError:
        msg = f"cannot unlock {key}: wrong passphrase"
        raise ConfigError(msg) from None


def ca_key_encrypted(ca: Path) -> bool:
    """Whether the CA directory's ca.key is passphrase-protected."""
    with (ca / "ca.key").open() as handle:
        return handle.readline().strip() == _ENCRYPTED_PEM


def create_ca(
    out: Path, *, passphrase: str | None, cn: str = CA_CN, days: int = CA_DAYS
) -> KeyPair:
    """Create the CA; with a passphrase its key is AES-256 encrypted (PBKDF2-SHA256)."""
    if passphrase is not None and len(passphrase) < MIN_PASSPHRASE:
        msg = f"the CA passphrase must be at least {MIN_PASSPHRASE} characters"
        raise ConfigError(msg)
    _prepare(out, ("ca.key", "ca.pem", "ca.der"))
    key, pem = out / "ca.key", out / "ca.pem"
    _write_ca_key(key, passphrase)
    extensions = [arg for ext in _CA_EXTENSIONS for arg in ("-addext", ext)]
    passin, stdin = _passin(passphrase)
    run(
        [
            "openssl", "req", "-x509", "-new", "-key", str(key), *passin, "-sha256",
            "-days", str(days), "-subj", f"/CN={cn}", "-out", str(pem), *extensions,
        ],
        stdin=stdin,
    )  # fmt: skip
    return _finish(key, pem, out / "ca.der")


def create_leaf(
    ca: Path, out: Path, *, ca_passphrase: str | None, cn: str = LEAF_CN, days: int = LEAF_MAX_DAYS
) -> KeyPair:
    """Issue a signing leaf; its key stays unencrypted because builds use it unattended."""
    if days > LEAF_MAX_DAYS:
        msg = f"leaf validity must be at most {LEAF_MAX_DAYS} days, got {days}"
        raise ConfigError(msg)
    for name in ("ca.key", "ca.pem"):
        if not (ca / name).is_file():
            msg = f"{ca / name} not found; create the CA with `zefosator keys ca` first"
            raise ConfigError(msg)
    _unlock_check(ca / "ca.key", ca_passphrase, encrypted=ca_key_encrypted(ca))
    _prepare(out, ("signing.key", "signing.pem", "signing.der"))
    key, pem = out / "signing.key", out / "signing.pem"
    _private_file(key)
    passin, stdin = _passin(ca_passphrase)
    with tempfile.TemporaryDirectory() as tmp:
        csr, ext = Path(tmp) / "leaf.csr", Path(tmp) / "leaf.ext"
        ext.write_text("\n".join(_LEAF_EXTENSIONS) + "\n")
        run(
            [
                "openssl", "req", "-new", "-newkey", "rsa:4096", "-nodes", "-sha256",
                "-subj", f"/CN={cn}", "-keyout", str(key), "-out", str(csr),
            ]
        )  # fmt: skip
        run(
            [
                "openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca / "ca.pem"),
                "-CAkey", str(ca / "ca.key"), *passin, "-set_serial", f"0x{secrets.token_hex(16)}",
                "-days", str(days), "-sha256", "-extfile", str(ext), "-out", str(pem),
            ],
            stdin=stdin,
        )  # fmt: skip
    return _finish(key, pem, out / "signing.der")
