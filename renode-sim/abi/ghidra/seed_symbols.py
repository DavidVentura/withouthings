# Ghidra pre-script: shape the raw app image before analysis.
#
# Run by abi/ghidra/analyze.sh through analyzeHeadless; not useful standalone.
# It reads abi/out/ghidra/seed.json (abi/ghidra/seed.py writes it) whose path
# arrives as the script argument.
#
# Everything here is evidence the repo already holds, plus two things the raw
# binary loader cannot know: the image is Thumb throughout, and 0x27000 is a
# Cortex-M vector table whose odd words are handler entry points.
# @category HWA10

import json
import re

from java.math import BigInteger
from ghidra.app.cmd.disassemble import ArmDisassembleCommand
from ghidra.program.model.address import AddressSet
from ghidra.program.model.data import (ArrayDataType, ByteDataType, CategoryPath,
                                       PointerDataType, StructureDataType,
                                       UnsignedIntegerDataType, UnsignedShortDataType)
from ghidra.program.model.symbol import SourceType

APP_BASE = 0x27000
VECTOR_WORDS = 64          # 16 system + 48 nRF52840 IRQ vectors

# Regions the app talks to that are not in the program's bytes. Calls and pool
# words landing here are external by construction, which is what makes them
# classifiable in the export.
EXTERNAL_BLOCKS = [
    ("mbr_softdevice", 0x00000000, 0x27000),
    ("sram", 0x20000000, 0x40000),
    ("peripherals", 0x40000000, 0x10000000),
    ("ppb", 0xE0000000, 0x100000),
]

SEED_TYPES = {
    "u32": UnsignedIntegerDataType(), "i32": UnsignedIntegerDataType(),
    "u16": UnsignedShortDataType(), "i16": UnsignedShortDataType(),
    "u8": ByteDataType(), "i8": ByteDataType(), "char": ByteDataType(),
}


# The manifest writes a length however it reads best at the declaration, and a
# run measured off an address is written in hex: `u8[0x23a4]` used to match
# nothing here and seed nothing, while abi/gen.py's own ARRAY accepted both all
# along, so the C header had the array and Ghidra did not.
FIELD_ARRAY = re.compile(r"^(.*)\[(0x[0-9a-fA-F]+|\d+)\]$")


def field_type(t):
    t = t.strip()
    if t.endswith("*"):
        return PointerDataType()
    m = FIELD_ARRAY.match(t)
    if m:
        base = field_type(m.group(1))
        return None if base is None else ArrayDataType(base, int(m.group(2), 0),
                                                       base.getLength())
    return SEED_TYPES.get(t)


def addr(value):
    return currentProgram.getAddressFactory().getDefaultAddressSpace().getAddress(value)


def app_range():
    block = currentProgram.getMemory().getBlock(addr(APP_BASE))
    return block.getStart(), block.getEnd()


def make_thumb(start, end):
    ctx = currentProgram.getProgramContext()
    ctx.setValue(ctx.getRegister("TMode"), start, end, BigInteger.ONE)


def add_external_blocks():
    mem = currentProgram.getMemory()
    for name, base, size in EXTERNAL_BLOCKS:
        if mem.getBlock(addr(base)) is not None:
            continue
        block = mem.createUninitializedBlock(name, addr(base), size, False)
        block.setRead(True)
        block.setWrite(name in ("sram", "peripherals", "ppb"))
        block.setExecute(name == "mbr_softdevice")


def force_function(value, name):
    """Disassemble Thumb at `value` and make it a function called `name`."""
    at = addr(value)
    cmd = ArmDisassembleCommand(at, None, True)
    cmd.applyTo(currentProgram, monitor)
    fn = getFunctionAt(at)
    if fn is None:
        fn = createFunction(at, name)
    if fn is None:
        return False
    fn.setName(name, SourceType.USER_DEFINED)
    return True


def apply_table(entry, category):
    fields = entry.get("fields")
    if not fields:
        createLabel(addr(entry["address"]), entry["name"], True, SourceType.USER_DEFINED)
        return
    struct = StructureDataType(category, entry["entry"], 0)
    for ftype, fname in fields:
        dt = field_type(ftype)
        if dt is None:
            raise RuntimeError("seed table %s: unmapped field type %r" % (entry["name"], ftype))
        struct.add(dt, dt.getLength(), fname, None)
    if struct.getLength() != entry["stride"]:
        raise RuntimeError("seed table %s: struct is %d bytes, manifest stride is %d"
                           % (entry["name"], struct.getLength(), entry["stride"]))
    array = ArrayDataType(struct, entry["count"], struct.getLength())
    at = addr(entry["address"])
    clearListing(at, at.add(array.getLength() - 1))
    createData(at, array)
    createLabel(at, entry["name"], True, SourceType.USER_DEFINED)


def seed_vector_table():
    """Word 0 is the initial SP; every odd word after it is a handler entry."""
    start = addr(APP_BASE)
    createLabel(start, "app_vector_table", True, SourceType.USER_DEFINED)
    vectors = ArrayDataType(PointerDataType(), VECTOR_WORDS - 1, 4)
    createData(start.add(4), vectors)
    createLabel(start.add(4), "app_vector_handlers", True, SourceType.USER_DEFINED)
    handlers = 0
    for i in range(1, VECTOR_WORDS):
        word = getInt(start.add(4 * i)) & 0xFFFFFFFF
        if word == 0 or not word & 1:
            continue
        target = word & ~1
        if not app_start <= target <= app_last.getOffset():
            continue
        name = "Reset_Handler" if i == 1 else "vector_%d_handler" % i
        known = getFunctionAt(addr(target))
        if known is not None and known.getSymbol().getSource() != SourceType.DEFAULT:
            name = known.getName()      # a seeded name beats the vector number
        if force_function(target, name):
            currentProgram.getSymbolTable().addExternalEntryPoint(addr(target))
            handlers += 1
    return handlers


def seed_svc_wrappers(start, end):
    """`svc #N; bx lr` SoftDevice wrappers: functions, and they do return.

    Ghidra sees the SVC as a black box and would otherwise leave the two
    halfwords as an unreachable island in the middle of the wrapper table.
    """
    size = int(end.subtract(start)) + 1
    data = [b & 0xFF for b in getBytes(start, size)]
    found = 0
    for off in range(0, size - 3, 2):
        if data[off + 1] == 0xDF and data[off + 2] == 0x70 and data[off + 3] == 0x47:
            if force_function(start.getOffset() + off, "svc_%d_wrapper" % data[off]):
                found += 1
    return found


args = getScriptArgs()
if len(args) != 1:
    raise RuntimeError("seed_symbols.py takes the seed json path")
with open(args[0]) as fh:
    seed = json.load(fh)

app_first, app_last = app_range()
app_start = app_first.getOffset()
make_thumb(app_first, app_last)
add_external_blocks()

hwa10_category = CategoryPath("/hwa10")
counts = {"functions": 0, "labels": 0, "globals": 0, "tables": 0}

for fn in seed["functions"]:
    if force_function(fn["address"], fn["name"]):
        counts["functions"] += 1
for lab in seed["labels"]:
    createLabel(addr(lab["address"]), lab["name"], True, SourceType.USER_DEFINED)
    counts["labels"] += 1
for g in seed["data"]:
    dt = field_type(g["type"])
    if dt is not None:
        at = addr(g["address"])
        clearListing(at, at.add(dt.getLength() - 1))
        createData(at, dt)
    createLabel(addr(g["address"]), g["name"], True, SourceType.USER_DEFINED)
    counts["globals"] += 1
for t in seed["tables"]:
    apply_table(t, hwa10_category)
    counts["tables"] += 1

vectors = seed_vector_table()
svc = seed_svc_wrappers(app_first, app_last)

# The decompiler's parameter ID pass costs more than the partition gains from it.
for option, value in [("Decompiler Parameter ID", "false"),
                      ("ARM Aggressive Instruction Finder", "true"),
                      ("Non-Returning Functions - Discovered", "true")]:
    setAnalysisOption(currentProgram, option, value)

println("seeded: %s, vectors %d, svc wrappers %d" % (counts, vectors, svc))
