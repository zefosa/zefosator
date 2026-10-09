"""Create the zefosa Secure Boot CA and issue signing leaf certificates (spec section 3)."""

import hashlib
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path

from zefosator.errors import ConfigError
from zefosator.worker.proc import run

CA_DAYS = 7300
LEAF_MAX_DAYS = 1095
CA_CN = "zefosa Secure Boot CA"
LEAF_CN = "zefosa signing key"
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


def create_ca(out: Path, *, cn: str = CA_CN, days: int = CA_DAYS) -> KeyPair:
    _prepare(out, ("ca.key", "ca.pem", "ca.der"))
    key, pem = out / "ca.key", out / "ca.pem"
    _private_file(key)
    extensions = [arg for ext in _CA_EXTENSIONS for arg in ("-addext", ext)]
    run(
        [
            "openssl", "req", "-x509", "-new", "-newkey", "rsa:4096", "-nodes", "-sha256",
            "-days", str(days), "-subj", f"/CN={cn}", "-keyout", str(key), "-out", str(pem),
            *extensions,
        ]
    )  # fmt: skip
    return _finish(key, pem, out / "ca.der")


def create_leaf(ca: Path, out: Path, *, cn: str = LEAF_CN, days: int = LEAF_MAX_DAYS) -> KeyPair:
    if days > LEAF_MAX_DAYS:
        msg = f"leaf validity must be at most {LEAF_MAX_DAYS} days, got {days}"
        raise ConfigError(msg)
    for name in ("ca.key", "ca.pem"):
        if not (ca / name).is_file():
            msg = f"{ca / name} not found; create the CA with `zefosator keys ca` first"
            raise ConfigError(msg)
    _prepare(out, ("signing.key", "signing.pem", "signing.der"))
    key, pem = out / "signing.key", out / "signing.pem"
    _private_file(key)
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
                "-CAkey", str(ca / "ca.key"), "-set_serial", f"0x{secrets.token_hex(16)}",
                "-days", str(days), "-sha256", "-extfile", str(ext), "-out", str(pem),
            ]
        )  # fmt: skip
    return _finish(key, pem, out / "signing.der")
