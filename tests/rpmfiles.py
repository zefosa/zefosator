"""Minimal RPM files for tests: a lead, an empty signature header and a main header."""

import struct
from pathlib import Path

LEAD_MAGIC = b"\xed\xab\xee\xdb"
HEADER_MAGIC = b"\x8e\xad\xe8\x01\x00\x00\x00\x00"
RPMTAG_BUILDTIME = 1006
RPM_INT32_TYPE = 4


def header(entries: list[tuple[int, int, bytes]]) -> bytes:
    """A header structure from (tag, type, data) entries, each with count 1."""
    index, store = b"", b""
    for tag, kind, data in entries:
        index += struct.pack(">iiii", tag, kind, len(store), 1)
        store += data
    return HEADER_MAGIC + struct.pack(">II", len(entries), len(store)) + index + store


def fake_rpm(path: Path, *, buildtime: int | None, payload: bytes = b"payload") -> Path:
    lead = LEAD_MAGIC + b"\x03\x00" + b"\x00" * 90
    signature = header([])  # 16 bytes: already 8-byte aligned
    entries = (
        []
        if buildtime is None
        else [(RPMTAG_BUILDTIME, RPM_INT32_TYPE, struct.pack(">I", buildtime))]
    )
    path.write_bytes(lead + signature + header(entries) + payload)
    return path
