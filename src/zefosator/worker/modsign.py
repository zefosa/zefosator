"""Linux kernel module signatures with the signer certificate embedded (spec section 6.2).

The kernel finds the signer either in its keyrings or, as here, among the certificates carried in
the PKCS#7 message, and then walks to a trusted key: the zefosa CA in the .machine keyring.
scripts/sign-file never embeds certificates, hence this implementation.
"""

import struct
import subprocess
from pathlib import Path

from zefosator.errors import BuildError

MODULE_SIG_MAGIC = b"~Module signature appended~\n"
PKEY_ID_PKCS7 = 2
_INFO = struct.Struct(">BBBBB3xI")  # struct module_signature


def strip_module_signature(data: bytes) -> bytes:
    if not data.endswith(MODULE_SIG_MAGIC):
        return data
    end = len(data) - len(MODULE_SIG_MAGIC) - _INFO.size
    sig_len = _INFO.unpack(data[end : end + _INFO.size])[5]
    return data[: end - sig_len]


def sign_module(data: bytes, key: Path, cert: Path) -> bytes:
    """Return `data` (any existing signature removed) with a SHA-512 PKCS#7 signature appended."""
    body = strip_module_signature(data)
    argv = [
        "openssl", "cms", "-sign", "-binary", "-noattr", "-nosmimecap", "-md", "sha512",
        "-signer", str(cert), "-inkey", str(key), "-outform", "DER",
    ]  # fmt: skip
    try:
        result = subprocess.run(argv, input=body, capture_output=True, check=False)
    except OSError as exc:
        msg = f"cannot run openssl cms: {exc.strerror or exc}"
        raise BuildError(msg) from exc
    if result.returncode != 0 or not result.stdout:
        tail = result.stderr.decode(errors="replace").strip().splitlines()[-5:]
        msg = f"openssl cms failed to sign a module (exit {result.returncode}): {' '.join(tail)}"
        raise BuildError(msg)
    pkcs7 = result.stdout
    trailer = _INFO.pack(0, 0, PKEY_ID_PKCS7, 0, 0, len(pkcs7)) + MODULE_SIG_MAGIC
    return body + pkcs7 + trailer
