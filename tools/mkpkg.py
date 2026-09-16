#!/usr/bin/env python3
"""Rewrite a ScanWatch firmware package's application version, and its app.

    python3 tools/mkpkg.py --version 9999 hwa10_3411_Tf4fD4.bin out.bin
    python3 tools/mkpkg.py --version 9998 --appl out/appl-relinked.bin \
        hwa10_3411_Tf4fD4.bin out.bin

The package is `[header 0x44][appl][bl][sd]`; the header is the same structure
the watch's `fwblk` table carries, so the bank the watch writes is the package
byte for byte. The version lives twice: in the header's appl entry, and as the
u32 trailer of the appl image itself, which `get_fw_version` (0x36e70) reads
and the probe reply reports. Both move together, the appl CRC32 and the header
CRC32 are recomputed, and the length never changes -- a package of a different
size would exercise the bank layout rather than the hashing and bank logic.

`--appl` replaces the whole appl part with a binary that starts at 0x27000, the
relinked image's app+library block (abi/relink_image.py). The part grows, `bl`
and `sd` slide up behind it and their header addresses follow. The version
trailer is at the fixed address get_fw_version reads, not at the end of the
part, so it stays put as the part grows.
"""

import argparse
import hashlib
import struct
from binascii import crc32
from pathlib import Path

HEADER_VERSION = 1
IE_APPL = 1
IE_NAMES = {1: "appl", 4: "bl", 8: "sd", 10: "sig"}
APPL_BASE = 0x27000
# get_fw_version (0x36e70) loads the absolute address 0xf1178 -- the u32 after
# the build string -- rather than anything derived from the part length.
APPL_VERSION_ADDRESS = 0xF1178
# The appl image ends with [build string][u32 version]; the getter at 0x36e70
# returns that last word.
APPL_VERSION_TRAILER = 4
# A bank on the external flash starts at 0x6000 and the next at 0x11f000, and a
# bank is the package file verbatim (FIRMWARE.md, "The fwblk external table").
BANK_CAPACITY = 0x11F000 - 0x6000
# UICR.NRFFW[0] points the MBR at the bootloader here, and the bootloader's copy
# (0xfc78c) bounds the part only by the length the package declares.
BOOTLOADER_BASE = 0xFC000


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
    parser.add_argument("--appl", type=Path,
                        help="replace the appl part with this binary, which starts at 0x27000")
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

    trailer_offset = APPL_VERSION_ADDRESS - APPL_BASE
    replacement = arguments.appl.read_bytes() if arguments.appl else None
    if replacement is None:
        parts = {}
        appl_length = appl.length
    else:
        if len(replacement) % 4:
            raise SystemExit(
                f"the appl image is {len(replacement)} bytes; a part's CRC32 is taken over its"
                " length rounded up to a word, so a part that is not a whole number of words"
                " would hash bytes of the next part")
        if len(replacement) < trailer_offset + APPL_VERSION_TRAILER:
            raise SystemExit(
                f"the appl image is {len(replacement):#x} bytes, too short to hold the version"
                f" trailer get_fw_version reads at {APPL_VERSION_ADDRESS:#x}")
        if APPL_BASE + len(replacement) > BOOTLOADER_BASE:
            raise SystemExit(
                f"the appl image ends at {APPL_BASE + len(replacement):#x}, past the bootloader"
                f" at {BOOTLOADER_BASE:#x}; the bootloader copies the whole part to"
                f" {APPL_BASE:#x} and would overwrite itself")
        parts = {IE_APPL: replacement}
        appl_length = len(replacement)

    embedded = struct.unpack_from(
        "<I", parts.get(IE_APPL, source), (0 if parts else appl.addr) + trailer_offset)[0]
    if embedded != appl.version:
        raise SystemExit(
            f"the appl trailer holds {embedded}, not the header's {appl.version}"
        )

    # Parts are laid out back to back in header order and each one is described
    # by an address and a length, so a longer appl only slides the rest up.
    out = bytearray(source[:header_length])
    cursor = header_length
    for entry in sorted(entries, key=lambda e: e.addr):
        data = parts.get(entry.ie, source[entry.addr : entry.addr + entry.length])
        entry.addr = cursor
        entry.length = len(data)
        out += data
        cursor += len(data)
    if cursor > BANK_CAPACITY:
        raise SystemExit(
            f"the package is {cursor:#x} bytes and a bank holds {BANK_CAPACITY:#x}")

    struct.pack_into("<I", out, appl.addr + trailer_offset, arguments.version)
    appl.version = arguments.version
    for entry in entries:
        entry.crc = component_crc(out, entry)
        struct.pack_into("<IIII", out, entry.offset,
                         entry.addr, entry.length, entry.crc, entry.version)
    body_length = struct.unpack_from("<H", out, 2)[0]
    header_crc = crc32(bytes(out[: 4 + body_length]))
    struct.pack_into("<I", out, 4 + body_length, header_crc)
    arguments.destination.write_bytes(bytes(out))

    print(f"{arguments.source} -> {arguments.destination}  {len(out)} bytes ({len(out):#x})")
    for entry in entries:
        print(f"{entry.name:4} addr {entry.addr:#08x} len {entry.length:#08x}"
              f" crc32 {entry.crc:#010x}")
    print(f"appl version {embedded} -> {arguments.version},"
          f" trailer at {appl.addr + trailer_offset:#x}")
    print(f"header crc32 {header_crc:#010x}")
    print(f"sha1         {hashlib.sha1(bytes(out)).hexdigest()}")


if __name__ == "__main__":
    main()
