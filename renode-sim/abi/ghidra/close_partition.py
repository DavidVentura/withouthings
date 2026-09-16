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
# @category HWA10

from ghidra.app.plugin.core.analysis import AutoAnalysisManager
from ghidra.program.model.address import AddressSet

APP_BASE = 0x27000
APP_END = 0xF117C

# Thumb function prologues GCC emits: push {..., lr}, and the VFP variant that
# saves registers before it.
PROLOGUES = ("push", "stmdb", "vpush")

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


def claim_head(start, end, instrs):
    """Give the run's head an owner; returns "made", "extended" or None."""
    before = listing.getInstructionBefore(start)
    owner = fm.getFunctionContaining(before.getMinAddress()) if before is not None else None
    falls = (before is not None and before.getMaxAddress().add(1).equals(start)
             and before.hasFallthrough())
    if rm.hasReferencesTo(start) or falls is False \
            or instrs[0].getMnemonicString().lower().startswith(PROLOGUES):
        if createFunction(start, None) is not None:
            return "made"
    if falls and owner is not None:
        body = AddressSet(owner.getBody())
        body.add(AddressSet(start, end.subtract(1)))
        owner.setBody(body)
        return "extended"
    return None


def close_round():
    made, extended, absorbed, left = 0, 0, 0, []
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
            if outcome == "made":
                made += 1
            else:
                extended += 1
            instrs = [i for i in instrs if fm.getFunctionContaining(i.getMinAddress()) is None]
            if instrs:
                start = instrs[0].getMinAddress()
    return made, extended, absorbed, left


mgr = AutoAnalysisManager.getAnalysisManager(currentProgram)
# Cheap rounds analyse only what the round changed, which follows the new
# functions' own branches and pool words; a whole-program pass then finds the
# owners the change-only analysis does not reach (it found about 100 more
# functions than change-only rounds alone), and the outer loop runs until a full
# pass adds nothing. Full passes cost 15 s each, change-only rounds about 1 s.
round_no = 0
while True:
    while True:
        made, extended, absorbed, left = close_round()
        println("orphans round %d: %d new functions, %d joined the function above, "
                "%d already claimed, %d left" % (round_no, made, extended, absorbed, len(left)))
        round_no += 1
        if not made and not extended:
            break
        mgr.startAnalysis(monitor)
    mgr.reAnalyzeAll(None)
    mgr.startAnalysis(monitor)
    made, extended, absorbed, left = close_round()
    println("orphans after full pass: %d new functions, %d joined the function above, "
            "%d already claimed, %d left" % (made, extended, absorbed, len(left)))
    if not made and not extended:
        break
    mgr.startAnalysis(monitor)

for start, end in left[:20]:
    println("  left %s..%s" % (start, end))
println("functions now %d" % fm.getFunctionCount())
