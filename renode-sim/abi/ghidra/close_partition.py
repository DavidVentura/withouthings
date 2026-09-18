# Ghidra post-script: give every disassembled byte an owner, then re-analyse.
#
# Run by abi/ghidra/analyze.sh before classify_gaps.py and export_partition.py.
#
# The first analysis leaves runs of instructions that belong to no function,
# mostly bodies only reached through a table or a pool word. A run is a function
# start when something references it, when it opens with a prologue, or when the
# instruction above it does not fall into it, since a fall-through and a
# reference are the only two ways to enter an instruction. The same cut is made
# inside a run, which is what splits a chain of `movs r0,#N; bx lr` stubs into
# one function each. A run the function above falls into is that function's
# second range. Both readings are mechanical, and a run that supports
# neither is left alone and reported rather than guessed at. Each round is
# followed by a re-analysis, which walks the new functions' own branches and
# pool words and so brings their address constants into the candidate set.
#
# A switch case block is the exception to "referenced, therefore a function": it
# is entered only by branches from the switch it belongs to, and a `tbb`/`tbh`
# offset or a jump-table word only stays correct while the block sits with that
# switch, so it has to be part of it. A `bl`, a pointer word, a prologue or a
# seeded name each say the opposite -- a tail-called function is still a
# function -- and take precedence, which is what keeps hand-named entries like
# softdevice_enable and crown_int_handler out of the switch they follow.
# @category HWA10

import json

from ghidra.app.plugin.core.analysis import AutoAnalysisManager
from ghidra.program.model.address import AddressSet
from ghidra.program.model.symbol import SourceType

APP_BASE = 0x27000
APP_END = 0xF117C

# Thumb function prologues GCC emits: push {..., lr}, and the VFP variant that
# saves registers before it.
PROLOGUES = ("push", "stmdb", "vpush")

SEEDED = (SourceType.USER_DEFINED, SourceType.IMPORTED)

listing = currentProgram.getListing()
fm = currentProgram.getFunctionManager()
rm = currentProgram.getReferenceManager()
space = currentProgram.getAddressFactory().getDefaultAddressSpace()
app_set = AddressSet(space.getAddress(APP_BASE), space.getAddress(APP_END - 1))


def orphan_runs():
    runs, run = [], None
    for instr in listing.getInstructions(app_set, True):
        start = instr.getMinAddress()
        if fm.getFunctionContaining(start) is not None:
            run = None
            continue
        if run is not None and run[1].equals(start):
            run[1] = instr.getMaxAddress().add(1)
            run[2].append(instr)
            continue
        run = [start, instr.getMaxAddress().add(1), [instr]]
        runs.append(run)
    return runs


def is_seeded(at):
    sym = getSymbolAt(at)
    return sym is not None and sym.getSource() in SEEDED


def entries(at):
    """How `at` is entered: (direct calls, pointer words, branching functions).

    The pointer words come back as addresses, because a word inside the
    switch's own body is its jump table rather than an entry point.

    The instruction's flow is what separates the three, not the reference type
    (Ghidra records a short `b` between two blocks as a call) and not the
    mnemonic (the `ldr pc` that reads a jump table is a branch, while the `ldr`
    that loads the same kind of word into a register is a pointer, and both
    carry the reference). A pointer word makes its target an entry point
    whatever else branches at it.
    """
    calls = 0
    pointers, owners = set(), set()
    for ref in rm.getReferencesTo(at):
        src = ref.getFromAddress()
        instr = listing.getInstructionAt(src)
        if instr is None:
            pointers.add(src.getOffset())
            continue
        flow = instr.getFlowType()
        if flow.isCall():
            calls += 1
            continue
        if not flow.isJump():
            pointers.add(src.getOffset())
            continue
        owner = fm.getFunctionContaining(src)
        owners.add(owner.getEntryPoint() if owner is not None else None)
    return calls, pointers, owners


def switch_owner(at, instrs):
    """The one function `at` is a case block of, or None if it is a function.

    Several owners means the block sits in the hole one function's body leaves
    inside its own span while another switch also branches at it; the one whose
    span brackets the block is the only one that can hold it contiguously.

    A pointer word only says "entry point" when it is somewhere else: the words
    of an inline jump table sit in the switch's own span, and moving the block
    away from them is exactly what must not happen.
    """
    if is_seeded(at) or instrs[0].getMnemonicString().lower().startswith(PROLOGUES):
        return None
    calls, pointers, owners = entries(at)
    if calls or not owners or None in owners:
        return None
    if len(owners) > 1:
        bracketing = set()
        for e in owners:
            fn = fm.getFunctionAt(e)
            if fn is not None and e.compareTo(at) < 0 \
                    and fn.getBody().getMaxAddress().compareTo(at) > 0:
                bracketing.add(e)
        owners = bracketing
        if len(owners) != 1:
            return None
    owner = fm.getFunctionAt(list(owners)[0])
    body = owner.getBody()
    span = (body.getMinAddress().getOffset(), body.getMaxAddress().getOffset())
    if any(not span[0] <= p <= span[1] for p in pointers):
        return None
    return owner


def absorb(fn, start, end):
    body = AddressSet(fn.getBody())
    body.add(AddressSet(start, end.subtract(1)))
    fn.setBody(body)


def claim_head(start, end, instrs):
    """Give the run's head an owner; returns "made", "extended", "case" or None."""
    owner_fn = switch_owner(start, instrs)
    if owner_fn is not None:
        # Only the head's own block: the rest of the run is re-examined, since a
        # run often holds case blocks of two different switches back to back.
        stop = end
        for instr in instrs[1:]:
            at = instr.getMinAddress()
            other = switch_owner(at, [instr])
            if rm.hasReferencesTo(at) and (
                    other is None or not other.getEntryPoint().equals(owner_fn.getEntryPoint())):
                stop = at
                break
        absorb(owner_fn, start, stop)
        return "case"
    before = listing.getInstructionBefore(start)
    owner = fm.getFunctionContaining(before.getMinAddress()) if before is not None else None
    falls = (before is not None and before.getMaxAddress().add(1).equals(start)
             and before.hasFallthrough())
    if rm.hasReferencesTo(start) or falls is False \
            or instrs[0].getMnemonicString().lower().startswith(PROLOGUES):
        if createFunction(start, None) is not None:
            return "made"
    if falls and owner is not None:
        absorb(owner, start, end)
        return "extended"
    return None


def close_round():
    counts = {"made": 0, "extended": 0, "case": 0}
    absorbed, left = 0, []
    for start, end, instrs in orphan_runs():
        if list(fm.getFunctionsOverlapping(AddressSet(start, end.subtract(1)))):
            # An earlier extension in this round already took these bytes.
            absorbed += 1
            continue
        # A run is usually several back-to-back functions (a chain of
        # constant-returning stubs, say), and a claimed head owns only its own
        # body, so the rest of the run is re-examined from the first instruction
        # still without an owner rather than left for the next round.
        while instrs:
            outcome = claim_head(start, end, instrs)
            if outcome is None:
                left.append((start, end))
                break
            counts[outcome] += 1
            instrs = [i for i in instrs if fm.getFunctionContaining(i.getMinAddress()) is None]
            if instrs:
                start = instrs[0].getMinAddress()
    return counts, absorbed, left


def dissolve_case_functions():
    """Undo the case blocks Ghidra's own analysers turned into functions.

    The test is the one claim_head uses.
    """
    dissolved = 0
    while True:
        moved = 0
        for fn in list(fm.getFunctions(app_set, True)):
            at = fn.getEntryPoint()
            if not fn.getName().startswith("caseD_") or is_seeded(at):
                continue
            body = AddressSet(fn.getBody())
            instr = listing.getInstructionAt(at)
            if instr is None:
                continue
            owner_fn = switch_owner(at, [instr])
            if owner_fn is None or owner_fn.getEntryPoint().equals(at):
                continue
            fm.removeFunction(at)
            owner_body = AddressSet(owner_fn.getBody())
            owner_body.add(body)
            owner_fn.setBody(owner_body)
            moved += 1
        dissolved += moved
        if not moved:
            return dissolved


def report(label, counts, absorbed, left):
    println("%s: %d new functions, %d joined the function above, %d case blocks, "
            "%d already claimed, %d left"
            % (label, counts["made"], counts["extended"], counts["case"], absorbed, len(left)))


PROLOGUE_HALFWORDS = (0xE92D, 0xED2D)


def halfword(at):
    return (getByte(at) & 0xFF) | ((getByte(at.add(1)) & 0xFF) << 8)


def repair_split_instructions():
    """Re-decode where the analysis cut a 32-bit instruction in half.

    A word that looks like a function pointer but is not does it: 0xbe6b0 holds
    0x00040001, which is a pair of 16-bit fields, and the analysis takes it for
    an entry point and disassembles from 0x40000 -- the second halfword of the
    `mov.w` at 0x3fffe. The export then has no instruction there, no relocation
    is emitted for whatever the hole holds, and a moved layout keeps the
    original displacement.

    The shape is unambiguous: two undefined bytes between two instructions whose
    halfword is a 32-bit Thumb prefix are the head of an instruction, so the
    thing that starts on its second halfword is wrong and is cleared.

    A hand-written address is not repaired but refused. symbols.txt and the
    manifests are authored, an address in them that lands inside an instruction
    is a transcription error, and papering over it would leave the wrong name on
    the wrong bytes for everything downstream that joins on the address.
    """
    # Ghidra hands undefined bytes back one at a time, so the run is what has to
    # be measured, not the code unit.
    runs, run = [], None
    for cu in listing.getCodeUnits(app_set, True):
        start = cu.getMinAddress()
        if listing.getInstructionAt(start) is not None or cu.isDefined():
            if run is not None:
                runs.append(run)
                run = None
            continue
        if run is not None and run[1].equals(start):
            run[1] = cu.getMaxAddress().add(1)
            continue
        run = [start, cu.getMaxAddress().add(1)]
    if run is not None:
        runs.append(run)

    holes = []
    for start, end in runs:
        if end.subtract(start) != 2 or start.getOffset() % 2:
            continue
        if (listing.getInstructionContaining(start.subtract(1)) is None
                or listing.getInstructionAt(end) is None):
            continue
        if halfword(start) & 0xF800 not in (0xE800, 0xF000, 0xF800):
            continue
        holes.append(start)
    seeded = []
    for start in holes:
        after = listing.getInstructionAt(start.add(2))
        if is_seeded(after.getMinAddress()):
            seeded.append((start, after.getMinAddress(),
                           getSymbolAt(after.getMinAddress()).getName()))
            continue
        if fm.getFunctionAt(after.getMinAddress()) is not None:
            removeFunctionAt(after.getMinAddress())
        listing.clearCodeUnits(after.getMinAddress(), after.getMaxAddress(), False)
        disassemble(start)
    for start, at, name in seeded:
        println("ERROR %s is seeded at %s, which is the second halfword of the"
                " instruction at %s; correct the address it was written down at"
                % (name, at, start))
    if seeded:
        raise RuntimeError("%d seeded addresses are not instruction boundaries"
                           % len(seeded))
    println("split instructions repaired: %d" % (len(holes) - len(seeded)))


def opens_with_prologue(at):
    """Does the halfword at `at` encode a Thumb function prologue?"""
    hw = (getByte(at) & 0xFF) | ((getByte(at.add(1)) & 0xFF) << 8)
    return hw & 0xFE00 == 0xB400 or hw in PROLOGUE_HALFWORDS


def disassemble_code_holes():
    """Disassemble the undefined runs that are code the first pass did not reach.

    Two shapes, and nothing else. A hole with an instruction on each side is
    code by construction: the run above it either falls through into it or
    branches over it, and in both cases the bytes are executed. A hole that
    opens with a prologue and ends exactly where an instruction begins is a
    function entry the analysis started two bytes late -- `0x37050` is
    `push {r3,r4,r5,lr}` and Ghidra begins the function at `0x37052` -- and
    leaving it undefined puts the entry and the body in different sections, so
    a layout that moves the body leaves the entry behind.

    Both readings are mechanical. Alignment padding is zeros or ones, which is
    not a prologue, and a rodata run would have to both open with a `push` and
    end on the first byte of an instruction.
    """
    runs, run, previous = [], None, None
    for cu in listing.getCodeUnits(app_set, True):
        start = cu.getMinAddress()
        if listing.getInstructionAt(start) is not None:
            if run is not None:
                runs.append(run)
                run = None
            previous = "code"
            continue
        if cu.isDefined():
            if run is not None:
                runs.append(run)
            run, previous = None, "data"
            continue
        if run is not None and run[1].equals(start):
            run[1] = cu.getMaxAddress().add(1)
            continue
        run = [start, cu.getMaxAddress().add(1), previous]
    if run is not None:
        runs.append(run)

    made = entries = 0
    for start, end, previous in runs:
        # Both shapes need code on the far side: a run that trails off into data
        # is data, whatever precedes it.
        if listing.getInstructionAt(end) is None:
            continue
        if previous != "code":
            if end.subtract(start) < 2 or start.getOffset() % 2:
                continue
            if not opens_with_prologue(start):
                continue
            entries += 1
        if disassemble(start):
            made += 1
    println("code holes disassembled: %d of %d (%d of them a function entry the"
            " analysis started late)" % (made, len(runs), entries))


mgr = AutoAnalysisManager.getAnalysisManager(currentProgram)
repair_split_instructions()
disassemble_code_holes()
mgr.startAnalysis(monitor)
# Cheap rounds analyse only what the round changed, which follows the new
# functions' own branches and pool words; a whole-program pass then finds the
# owners the change-only analysis does not reach (it found about 100 more
# functions than change-only rounds alone), and the outer loop runs until a full
# pass adds nothing. Full passes cost 15 s each, change-only rounds about 1 s.
round_no = 0
while True:
    while True:
        counts, absorbed, left = close_round()
        report("orphans round %d" % round_no, counts, absorbed, left)
        round_no += 1
        if not any(counts.values()):
            break
        mgr.startAnalysis(monitor)
    mgr.reAnalyzeAll(None)
    mgr.startAnalysis(monitor)
    counts, absorbed, left = close_round()
    report("orphans after full pass", counts, absorbed, left)
    if not any(counts.values()):
        break
    mgr.startAnalysis(monitor)

def remove_string_functions():
    """Undo the functions the analysis made out of string bytes.

    A tail-shared string is the trap: "BEFORE_PREDICTED_OVULATION" and
    "PREDICTED_OVULATION" are one run of bytes with two pointers into it, the
    string analyzer types only the suffix, and the prefix is left over for
    something else to claim. A function made of such bytes is not code, and
    moving it as one takes the head of a string away from its tail.

    The rule is narrow enough that it cannot fire on real code: the body has to
    be printable ASCII and NUL throughout, nothing may call it, and it has to
    either contain a NUL-terminated printable run or run straight into a string
    the analyzer did type. The image's four-byte `movs r0,#0; bx lr` stubs are
    all printable too, which is why the call graph is part of the rule.
    """
    called = set()
    for fn in fm.getFunctions(app_set, True):
        for ref in rm.getReferencesTo(fn.getEntryPoint()):
            if ref.getReferenceType().isCall():
                called.add(fn.getEntryPoint().getOffset())
    removed = []
    for fn in list(fm.getFunctions(app_set, True)):
        start = fn.getEntryPoint().getOffset()
        if start in called or fn.getBody().getNumAddressRanges() != 1:
            continue
        end = fn.getBody().getMaxAddress().getOffset() + 1
        body = bytearray(b & 0xFF for b in
                         getBytes(space.getAddress(start), end - start))
        if not body or any(not (32 <= c < 127 or c == 0) for c in body):
            continue
        terminated = any(body[i] == 0 and 32 <= body[i - 1] < 127
                         and 32 <= body[i - 2] < 127 for i in range(2, len(body)))
        after = listing.getDataAt(space.getAddress(end))
        follows_string = (after is not None and after.isDefined()
                          and after.getDataType().getName().lower().startswith(
                              ("string", "unicode", "char")))
        if not (terminated or follows_string):
            continue
        removed.append((start, end))
    for start, end in removed:
        removeFunctionAt(space.getAddress(start))
        listing.clearCodeUnits(space.getAddress(start),
                               space.getAddress(end - 1), False)
    println("functions that were string bytes removed: %d (%d bytes)"
            % (len(removed), sum(e - s for s, e in removed)))


def remove_data_named_functions():
    """Undo the functions the analysis made out of data no execution can enter.

    This image is Thumb throughout, so a word holding an even address in the
    app names data, never an instruction. Where such a word names the inside of
    a function that nothing calls, nothing branches to and no word names with
    the Thumb bit, both readings cannot be right, and the one with evidence for
    it wins: the bytes are a record table or a float table the linear pass
    decoded. Leaving it as code is not neutral -- the pointer words inside it
    are covered by the disassembly, so the relayout moves the table without
    relocating anything it holds, and the pool word naming it reads as a number.
    """
    raw = getBytes(space.getAddress(APP_BASE), APP_END - APP_BASE)
    even, odd = set(), set()
    for at in range(0, len(raw) - 3, 4):
        v = ((raw[at] & 0xFF) | ((raw[at + 1] & 0xFF) << 8)
             | ((raw[at + 2] & 0xFF) << 16) | ((raw[at + 3] & 0xFF) << 24))
        if APP_BASE <= (v & ~1) < APP_END:
            (odd if v & 1 else even).add(v & ~1)

    removed = []
    for fn in fm.getFunctions(app_set, True):
        entry = fn.getEntryPoint()
        start = entry.getOffset()
        if is_seeded(entry) or start in odd:
            continue
        if any(r.getReferenceType().isFlow()
               for r in rm.getReferencesTo(entry)):
            continue
        body = fn.getBody()
        lo = body.getMinAddress().getOffset()
        hi = body.getMaxAddress().getOffset() + 1
        if not any(a in even for a in range(lo + (-lo) % 4, hi, 4)):
            continue
        if any(a in odd for a in range(lo, hi)):
            continue
        removed.append((lo, hi))
    for lo, hi in removed:
        area = AddressSet(space.getAddress(lo), space.getAddress(hi - 1))
        # A body decoded out of data has other decoded bodies inside it; a
        # function left behind with no instructions under it makes the export's
        # cover claim the same bytes as a function and as a gap.
        for fn in list(fm.getFunctionsOverlapping(area)):
            removeFunctionAt(fn.getEntryPoint())
        listing.clearCodeUnits(space.getAddress(lo), space.getAddress(hi - 1),
                               False)
    println("functions a word names as data removed: %d (%d bytes)"
            % (len(removed), sum(e - s for s, e in removed)))


def declared_data_pointers(seed_path):
    """Every address a declared table's pointer-to-data field holds.

    A declared table's row struct says what each of its columns points at. A
    column pointing at code, or at nothing, is a handler and the analysis is
    right to follow it; a column pointing at a scalar is data, and its target is
    data at whatever address it lands on. That distinction is the only way to
    read the
    asset table's `bits`, which points at 1-bit rows that start on an odd byte
    as often as an even one: the parity test that decides everything else in
    this file reads an odd one as a Thumb entry and lets the analysis
    disassemble a bitmap.
    """
    with open(seed_path) as fh:
        seed = json.load(fh)
    targets = set()
    for table in seed["tables"]:
        wanted = [f["offset"] for f in table.get("fields") or []
                  if f["points_to"] in ("char", "data")]
        for row in range(table["count"]):
            base = table["address"] + row * table["stride"]
            for at in wanted:
                targets.add(getInt(space.getAddress(base + at)) & 0xFFFFFFFF)
    return targets


def remove_declared_data_functions(targets):
    """Undo the disassembly of the data a declared table points at."""
    removed = []
    for target in sorted(targets):
        if not APP_BASE <= target < APP_END:
            continue
        fn = fm.getFunctionContaining(space.getAddress(target))
        if fn is None or is_seeded(fn.getEntryPoint()):
            continue
        body = fn.getBody()
        lo = body.getMinAddress().getOffset()
        hi = body.getMaxAddress().getOffset() + 1
        area = AddressSet(space.getAddress(lo), space.getAddress(hi - 1))
        for inner in list(fm.getFunctionsOverlapping(area)):
            removeFunctionAt(inner.getEntryPoint())
        listing.clearCodeUnits(space.getAddress(lo), space.getAddress(hi - 1),
                               False)
        removed.append((lo, hi))
    println("functions a declared data pointer names removed: %d (%d bytes)"
            % (len(removed), sum(e - s for s, e in removed)))


def split_named_prologues():
    """Cut a function in two where a word names a prologue inside its body.

    The closing rounds only ever see code with no owner, so a function entry
    the first analysis swallowed into the function above is never re-examined:
    Ghidra extended the previous body over it and the run is not an orphan.
    The bytes say otherwise on the same two grounds claim_head decides an
    orphan head by, and both have to hold at once. The instruction above it
    does not fall into it (an unconditional branch, a `bx lr`, a `pop {..,pc}`
    or a literal pool), so the only way in is a reference; and it opens with a
    push, which is a function prologue and not something a compiler emits in
    the middle of a body. The reference itself is the third: a 4-aligned word
    outside the disassembly holding the address with bit 0 set, which is how
    this image spells a Thumb function pointer.

    Three conditions is not one too many. A word that is an instruction
    boundary below a branch and nothing else is far commoner than a function
    pointer here -- 91 words in the image satisfy that much, and they are the
    low halfword of a float, `0x3ffff`, `0xa7525` -- so the prologue is what
    separates the class from the coincidences.
    """
    raw = getBytes(space.getAddress(APP_BASE), APP_END - APP_BASE)
    targets = {}
    for at in range(0, len(raw) - 3, 4):
        value = ((raw[at] & 0xFF) | ((raw[at + 1] & 0xFF) << 8)
                 | ((raw[at + 2] & 0xFF) << 16) | ((raw[at + 3] & 0xFF) << 24))
        if not value & 1 or not APP_BASE <= value - 1 < APP_END:
            continue
        word = space.getAddress(APP_BASE + at)
        if listing.getInstructionContaining(word) is not None:
            continue
        targets.setdefault(value - 1, []).append(APP_BASE + at)

    split = []
    for target, named_by in sorted(targets.items()):
        at = space.getAddress(target)
        if listing.getInstructionAt(at) is None or not opens_with_prologue(at):
            continue
        owner = fm.getFunctionContaining(at)
        if owner is None or owner.getEntryPoint().equals(at):
            continue
        before = listing.getInstructionBefore(at)
        if (before is not None and before.getMaxAddress().add(1).equals(at)
                and before.hasFallthrough()):
            continue
        body = AddressSet(owner.getBody())
        tail = body.getRangeContaining(at)
        if owner.getEntryPoint().compareTo(tail.getMaxAddress()) <= 0 \
                and owner.getEntryPoint().compareTo(at) >= 0:
            # The owner's own entry is in the tail: the body was assembled
            # backwards, and what to do with the head is a different argument
            # from this one.
            println("left 0x%x: the function at 0x%x has its entry inside the"
                    " range the split would take" % (target, owner.getEntryPoint().getOffset()))
            continue
        body.delete(AddressSet(at, tail.getMaxAddress()))
        owner.setBody(body)
        if createFunction(at, None) is None:
            raise RuntimeError("0x%x opens with a prologue below %s but no"
                               " function could be made there" % (target, before))
        split.append((target, owner.getEntryPoint().getOffset(), named_by,
                      before.toString() if before is not None else "a pool"))
    for target, owner, named_by, below in split:
        println("split 0x%x out of the function at 0x%x: a prologue below `%s`,"
                " named by %s" % (target, owner, below,
                                  ", ".join("0x%x" % w for w in named_by)))
    println("functions split out of a body a word names: %d" % len(split))


remove_string_functions()
remove_data_named_functions()
remove_declared_data_functions(declared_data_pointers(getScriptArgs()[0]))
# After the removals, so that taking the named prologue out of a body cannot
# leave the rest of that body looking like data no odd word names.
split_named_prologues()
mgr.startAnalysis(monitor)

# Last, because every full pass puts some of them back: the switch analyser
# promotes a case target to a function of its own, which closing rounds never
# see again because it is no longer an orphan.
println("case functions dissolved: %d" % dissolve_case_functions())

for start, end in left[:20]:
    println("  left %s..%s" % (start, end))
println("functions now %d" % fm.getFunctionCount())
