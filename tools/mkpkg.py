#!/usr/bin/env python3
"""Rewrite a ScanWatch firmware package's application version.

    python3 tools/mkpkg.py --version 9999 hwa10_3411_Tf4fD4.bin out.bin

The package is `[header 0x44][appl][bl][sd]`; the header is the same structure
the watch's `fwblk` table carries, so the bank the watch writes is the package
byte for byte. The version lives twice: in the header's appl entry, and as the
u32 trailer of the appl image itself, which `get_fw_version` (0x36e70) reads
and the probe reply reports. Both move together, the appl CRC32 and the header
CRC32 are recomputed, and the length never changes -- a package of a different
size would exercise the bank layout rather than the hashing and bank logic.
"""

import argparse
import hashlib
import struct
from binascii import crc32
from pathlib import Path

HEADER_VERSION = 1
IE_APPL = 1
IE_NAMES = {1: "appl", 4: "bl", 8: "sd", 10: "sig"}
# The appl image ends with [build string][u32 version]; the getter at 0x36e70
# returns that last word.
APPL_VERSION_TRAILER = 4


class Entry:
    def __init__(self, ie, offset, addr, length, crc, version):
        self.ie = ie
        self.offset = offset
        self.addr = addr
        self.length = length
        self.crc = crc
        self.version = version

    @property
    def name(self):
        return IE_NAMES.get(self.ie, f"ie{self.ie}")


def parse_header(data):
    version, body_length = struct.unpack_from("<HH", data)
    if version != HEADER_VERSION:
        raise SystemExit(f"header version {version}, expected {HEADER_VERSION}")
    total = 4 + body_length + 4
    stored_crc = struct.unpack_from("<I", data, 4 + body_length)[0]
    computed = crc32(data[: 4 + body_length])
    if stored_crc != computed:
        raise SystemExit(f"header CRC {stored_crc:#010x} != computed {computed:#010x}")

    entries = []
    offset = 4
    while offset < 4 + body_length:
        ie, length = struct.unpack_from("<HH", data, offset)
        offset += 4
        if ie not in IE_NAMES or length != 16:
            raise SystemExit(f"unexpected header entry ie={ie} len={length}")
        if ie == 10:
            raise SystemExit("this package is signed; rewriting it would invalidate the signature")
        addr, fw_len, crc, fw_version = struct.unpack_from("<IIII", data, offset)
        entries.append(Entry(ie, offset, addr, fw_len, crc, fw_version))
        offset += length
    return total, entries


def component_crc(data, entry):
    # The watch aligns the CRC length up to a word, as fw_parser.py does.
    length = (entry.length + 3) & ~3
    return crc32(data[entry.addr : entry.addr + length])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", type=int, required=True)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    if not 0 < arguments.version <= 0xFFFFFFFF:
        raise SystemExit(f"version {arguments.version} is not a u32")

    source = arguments.source.read_bytes()
    header_length, entries = parse_header(source)
    appl = next((e for e in entries if e.ie == IE_APPL), None)
    if appl is None:
        raise SystemExit("no appl entry in the header")
    if appl.addr != header_length:
        raise SystemExit(f"appl starts at {appl.addr:#x}, not right after the {header_length:#x} byte header")
    end = max(e.addr + e.length for e in entries)
    if end != len(source):
        raise SystemExit(f"the entries end at {end:#x} but the file is {len(source):#x} bytes")
    for entry in entries:
        stored = component_crc(source, entry)
        if stored != entry.crc:
            raise SystemExit(f"{entry.name} CRC {entry.crc:#010x} != computed {stored:#010x}")

    trailer = appl.addr + appl.length - APPL_VERSION_TRAILER
    embedded = struct.unpack_from("<I", source, trailer)[0]
    if embedded != appl.version:
        raise SystemExit(
            f"the appl trailer at {trailer:#x} holds {embedded}, not the header's {appl.version}"
        )

    out = bytearray(source)
    struct.pack_into("<I", out, trailer, arguments.version)
    struct.pack_into("<I", out, appl.offset + 12, arguments.version)
    appl.crc = component_crc(out, appl)
    struct.pack_into("<I", out, appl.offset + 8, appl.crc)
    body_length = struct.unpack_from("<H", out, 2)[0]
    header_crc = crc32(bytes(out[: 4 + body_length]))
    struct.pack_into("<I", out, 4 + body_length, header_crc)

    if len(out) != len(source):
        raise SystemExit("the rewrite changed the length")
    arguments.destination.write_bytes(bytes(out))

    print(f"{arguments.source} -> {arguments.destination}  {len(out)} bytes ({len(out):#x})")
    print(f"appl version {embedded} -> {arguments.version}, trailer at {trailer:#x}")
    print(f"appl crc32   {appl.crc:#010x}")
    print(f"header crc32 {header_crc:#010x}")
    print(f"sha1         {hashlib.sha1(bytes(out)).hexdigest()}")


if __name__ == "__main__":
    main()
