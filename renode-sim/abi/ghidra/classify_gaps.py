# Ghidra post-script: type what is left of the image's untyped bytes.
#
# Run by abi/ghidra/analyze.sh after close_partition.py.
#
# Only rules that cannot plausibly fire on something else are allowed to type a
# run, because a wrong type here becomes a wrong section and a wrong relocation
# later. Everything else stays untyped on purpose. The names are mechanical
# (pad_/ptrtab_/flttab_ plus the address) so the export can report the class
# without a second source of truth.
#
# The display anchor: the SSD1320 panel is 144x98 at 4bpp, so a full frame is
# 7056 bytes and a glyph table would be {width, height, rows...} records. Both
# are tested and neither fires on this image, which is consistent with the note
# in symbols.txt that the assets live in the external flash.
# @category HWA10

import struct

from ghidra.program.model.address import AddressSet
from ghidra.program.model.listing import Instruction
from ghidra.program.model.data import (ArrayDataType, ByteDataType, FloatDataType,
                                       PointerDataType, TerminatedStringDataType)
from ghidra.program.model.symbol import SourceType

APP_BASE = 0x27000
APP_END = 0xF117C
RAM_BASE, RAM_END = 0x20000000, 0x20040000
FRAME_BYTES = 144 * 98 // 2

MIN_STRING = 4
MIN_TABLE_WORDS = 4
MIN_FLOAT_WORDS = 8

listing = currentProgram.getListing()
space = currentProgram.getAddressFactory().getDefaultAddressSpace()
app_set = AddressSet(space.getAddress(APP_BASE), space.getAddress(APP_END - 1))
image = bytes(bytearray(b & 0xFF for b in getBytes(space.getAddress(APP_BASE), APP_END - APP_BASE)))


def addr(value):
    return space.getAddress(value)


def data(start, end):
    return image[start - APP_BASE:end - APP_BASE]


def is_padding(raw):
    return len(raw) >= 4 and (raw.count(raw[0:1]) == len(raw)) and raw[0] in (0x00, 0xFF)


def words(start, end):
    return struct.unpack_from("<%dI" % ((end - start) // 4), data(start, end), 0)


def is_pointer_table(start, end):
    """Every word an app or RAM address, or zero: no constant looks like that."""
    if start % 4 or (end - start) // 4 < MIN_TABLE_WORDS or (end - start) % 4:
        return False
    w = words(start, end)
    return all(v == 0 or APP_BASE <= v < APP_END or RAM_BASE <= v < RAM_END for v in w)


def is_float_table(start, end):
    if start % 4 or (end - start) // 4 < MIN_FLOAT_WORDS or (end - start) % 4:
        return False
    f = struct.unpack_from("<%df" % ((end - start) // 4), data(start, end), 0)
    plausible = sum(1 for v in f if v == 0.0 or 1e-6 < abs(v) < 1e6)
    return plausible == len(f) and any(v != 0.0 for v in f)


def glyph_records(start, end):
    """{width, height, rows} chained over the whole run, at 1bpp and at 4bpp."""
    best = 0
    for bpp in (1, 4):
        cur, recs = start, 0
        while cur + 2 < end:
            w, h = image[cur - APP_BASE], image[cur + 1 - APP_BASE]
            if not (4 <= w <= 64 and 4 <= h <= 64):
                break
            cur += 2 + (((w + 7) // 8) * h if bpp == 1 else (w * h + 1) // 2)
            recs += 1
        if cur >= end - 2 and recs >= 8:
            best = max(best, recs)
    return best


def strings_in(start, end):
    """NUL-terminated printable runs the string analyzer did not already take."""
    out = []
    raw = data(start, end)
    i = 0
    while i < len(raw):
        j = i
        while j < len(raw) and (32 <= raw[j] < 127 or raw[j] in (9, 10, 13)):
            j += 1
        if j - i >= MIN_STRING and j < len(raw) and raw[j] == 0:
            out.append((start + i, start + j + 1))
            i = j + 1
        else:
            i = j + 1 if j == i else j
    return out


runs, run = [], None
for cu in listing.getCodeUnits(app_set, True):
    start = cu.getMinAddress().getOffset()
    if isinstance(cu, Instruction) or cu.isDefined():
        run = None
        continue
    if run is not None and run[1] == start:
        run[1] = start + cu.getLength()
        continue
    run = [start, start + cu.getLength()]
    runs.append(run)

totals = {"padding": 0, "string": 0, "pointer_table": 0, "float_table": 0,
          "bitmap": 0, "untyped": 0}
frames = bitmaps = 0
for start, end in runs:
    raw = data(start, end)
    if (end - start) % FRAME_BYTES == 0 and end - start >= FRAME_BYTES:
        frames += 1
    if glyph_records(start, end):
        bitmaps += 1
    if is_padding(raw):
        array = ArrayDataType(ByteDataType(), end - start, 1)
        createData(addr(start), array)
        createLabel(addr(start), "pad_%x" % start, True, SourceType.ANALYSIS)
        totals["padding"] += end - start
        continue
    if is_pointer_table(start, end):
        array = ArrayDataType(PointerDataType(), (end - start) // 4, 4)
        createData(addr(start), array)
        createLabel(addr(start), "ptrtab_%x" % start, True, SourceType.ANALYSIS)
        totals["pointer_table"] += end - start
        continue
    if is_float_table(start, end):
        array = ArrayDataType(FloatDataType(), (end - start) // 4, 4)
        createData(addr(start), array)
        createLabel(addr(start), "flttab_%x" % start, True, SourceType.ANALYSIS)
        totals["float_table"] += end - start
        continue
    taken = 0
    for s, e in strings_in(start, end):
        createData(addr(s), TerminatedStringDataType())
        totals["string"] += e - s
        taken += e - s
    totals["untyped"] += (end - start) - taken

println("gaps: %d runs, %s, frame-sized runs %d, glyph-record runs %d"
        % (len(runs), totals, frames, bitmaps))
