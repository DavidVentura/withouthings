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


mgr = AutoAnalysisManager.getAnalysisManager(currentProgram)
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

# Last, because every full pass puts some of them back: the switch analyser
# promotes a case target to a function of its own, which closing rounds never
# see again because it is no longer an orphan.
println("case functions dissolved: %d" % dissolve_case_functions())

for start, end in left[:20]:
    println("  left %s..%s" % (start, end))
println("functions now %d" % fm.getFunctionCount())
