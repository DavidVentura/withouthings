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

# The seed carries a width and a shape, never a C spelling: the compiler has
# already read the header and the three shapes below are all Ghidra needs to lay
# the rows out. A scalar of a width Ghidra has no integer for stays bytes.
SCALARS = {4: UnsignedIntegerDataType(), 2: UnsignedShortDataType(),
           1: ByteDataType()}


def shape_type(shape):
    """The Ghidra data type of one seeded field, or None where there is none."""
    if shape["kind"] == "pointer":
        return PointerDataType()
    if shape["kind"] == "array":
        base = SCALARS.get(shape["unit"])
        return None if base is None else ArrayDataType(base, shape["count"],
                                                       base.getLength())
    return SCALARS.get(shape["size"])


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
    for shape in fields:
        dt = shape_type(shape)
        if dt is None:
            raise RuntimeError("seed table %s: no type for field %s"
                               % (entry["name"], shape["name"]))
        struct.add(dt, dt.getLength(), shape["name"], None)
    if struct.getLength() != entry["stride"]:
        raise RuntimeError("seed table %s: struct is %d bytes, manifest stride is %d"
                           % (entry["name"], struct.getLength(), entry["stride"]))
    array = ArrayDataType(struct, entry["count"], struct.getLength())
    at = addr(entry["address"])
    clearListing(at, at.add(array.getLength() - 1))
    createData(at, array)
    createLabel(at, entry["name"], True, SourceType.USER_DEFINED)


def seed_vector_table(known_names):
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
        # The map's name beats the vector number wherever the map has one,
        # whatever kind it holds the address as: five of these handlers are
        # `prose` labels rather than functions, and asking Ghidra whether a
        # function is there missed all five and let the number win.
        name = known_names.get(target)
        if name is None:
            name = "Reset_Handler" if i == 1 else "vector_%d_handler" % i
        if force_function(target, name):
            currentProgram.getSymbolTable().addExternalEntryPoint(addr(target))
            handlers += 1
    return handlers


def seed_svc_wrappers(start, end, known_names):
    """`svc #N; bx lr` SoftDevice wrappers: functions, and they do return.

    Ghidra sees the SVC as a black box and would otherwise leave the two
    halfwords as an unreachable island in the middle of the wrapper table.

    The number is only the name where the map has none. abi/symbols.yaml names
    every one of these from the SoftDevice's own call number (`sd_ble_gap_*`,
    `sd_flash_write`), and this used to rename them after the seed had been
    applied, so the export carried 60 `svc_N_wrapper` names for addresses the
    map spells properly.
    """
    size = int(end.subtract(start)) + 1
    data = [b & 0xFF for b in getBytes(start, size)]
    found = 0
    for off in range(0, size - 3, 2):
        if data[off + 1] == 0xDF and data[off + 2] == 0x70 and data[off + 3] == 0x47:
            at = start.getOffset() + off
            if force_function(at, known_names.get(
                    at, "svc_%d_wrapper" % data[off])):
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
# An attribution says which module a body belongs to and what established it,
# and says no name. It goes on as a comment: renaming the body would take it
# off the list of bodies still to read, which is the whole reason the rule that
# made it stopped producing a name.
for att in seed.get("attributions", ()):
    at = addr(att["address"])
    note = "module: %s\n%s" % (att.get("module") or "unknown",
                               att.get("evidence") or "")
    fn = getFunctionAt(at)
    if fn is not None:
        fn.setComment(note)
    else:
        setPlateComment(at, note)
    counts["attributions"] = counts.get("attributions", 0) + 1
for lab in seed["labels"]:
    createLabel(addr(lab["address"]), lab["name"], True, SourceType.USER_DEFINED)
    counts["labels"] += 1
for g in seed["data"]:
    # A global is laid out from its width and its count alone; a struct global
    # stays a label, because the partition reads its fields through the header
    # and Ghidra would only be repeating the declaration.
    dt = None
    if g["kind"] == "pointer":
        dt = PointerDataType()
    elif g["kind"] == "scalar":
        dt = SCALARS.get(g["size"] // g["count"])
    if dt is not None and g["count"] > 1:
        dt = ArrayDataType(dt, g["count"], dt.getLength())
    if dt is not None:
        at = addr(g["address"])
        clearListing(at, at.add(dt.getLength() - 1))
        createData(at, dt)
    createLabel(addr(g["address"]), g["name"], True, SourceType.USER_DEFINED)
    counts["globals"] += 1
for t in seed["tables"]:
    apply_table(t, hwa10_category)
    counts["tables"] += 1

# What the map calls each address, whatever kind it calls it: the two passes
# below run after the seed is applied, so without this they would rename what
# the seed had just named.
known_names = {}
for group in ("functions", "labels", "data", "tables"):
    for entry in seed[group]:
        known_names.setdefault(entry["address"], entry["name"])

vectors = seed_vector_table(known_names)
svc = seed_svc_wrappers(app_first, app_last, known_names)

# The decompiler's parameter ID pass costs more than the partition gains from it.
for option, value in [("Decompiler Parameter ID", "false"),
                      ("ARM Aggressive Instruction Finder", "true"),
                      ("Non-Returning Functions - Discovered", "true")]:
    setAnalysisOption(currentProgram, option, value)

println("seeded: %s, vectors %d, svc wrappers %d" % (counts, vectors, svc))
