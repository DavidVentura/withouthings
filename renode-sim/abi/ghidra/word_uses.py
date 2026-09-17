# Ghidra post-script: what the code does with every literal-pool word it loads.
#
# Run by abi/ghidra/analyze.sh after export_partition.py, whose references.json
# supplies the pool words and their reading sites. Writes word_uses.json.
#
# The structural signals (does the value land on a function start, on an item
# start) leave a word whose value lands in an untyped run undecided, and the
# only other end of the evidence is what the code does with the value. That is
# a forward data-flow: seed the varnode a pc-relative load defines with the
# address it read, propagate through copies and address arithmetic over the
# function's p-code, and record every operation that consumes it. An address
# used as a branch target or as a load/store base is a pointer; a value only
# compared or multiplied is a number.
#
# Alongside the use names the walk records the shape of the access: the constant
# displacement from the loaded address at which the code dereferences it, with
# the width of that load or store (`field+8:4`), and the constant an index is
# multiplied by before being added to it (`stride:12`). For a pool word that
# names a record table that is the table's layout, read off the reader's own
# address arithmetic, which is what abi/runs.py needs where a table has too few
# references to space out a grid.
#
# A value handed to a callee is the common case and says nothing on its own, so
# the same data flow is run once more with each function's four argument
# registers as its seeds: that gives every parameter the same use summary, and a
# fixpoint over the call graph closes "passed on to someone else" into what is
# finally done with it. The callee's own code is the prototype.
#
# Raw p-code rather than the decompiler's high p-code: the decompiler costs
# minutes over 5341 functions, and the question -- which op reads this
# register -- is answered before register allocation is undone.
# @category HWA10

import json
import os

from ghidra.program.model.address import AddressSet
from ghidra.program.model.pcode import PcodeOp

APP_BASE = 0x27000
APP_END = 0xF117C

out_dir = getScriptArgs()[0]
listing = currentProgram.getListing()
fm = currentProgram.getFunctionManager()
space = currentProgram.getAddressFactory().getDefaultAddressSpace()
app_set = AddressSet(space.getAddress(APP_BASE), space.getAddress(APP_END - 1))

with open(os.path.join(out_dir, "references.json")) as fh:
    refs = json.load(fh)

pool_targets = set()
for r in refs["pool_reads"]:
    pool_targets.add(r["target"])

ARG_REGS = ["r0", "r1", "r2", "r3"]


def in_app(v):
    return APP_BASE <= v < APP_END


def vnkey(vn):
    return (vn.getSpace(), vn.getOffset(), vn.getSize())


def regkey(name):
    reg = currentProgram.getRegister(name)
    return (reg.getAddress().getAddressSpace().getSpaceID(),
            reg.getAddress().getOffset(), reg.getMinimumByteSize())


arg_keys = [regkey(name) for name in ARG_REGS]
all_regs = [regkey("r%d" % i) for i in range(13)] + [regkey("sp"), regkey("lr"),
                                                    regkey("pc")]
# A call clobbers the caller-saved registers, so a value still sitting in one of
# them afterwards is not the value that was loaded; keeping it there attributed
# a string pointer's address to whatever the next instruction did with the
# returned value.
CLOBBERED = arg_keys + [regkey("r12"), regkey("lr")]

# Propagating ops: the value that comes out still names the same object.
COPY_OPS = frozenset([PcodeOp.COPY, PcodeOp.INT_ZEXT, PcodeOp.INT_SEXT,
                      PcodeOp.SUBPIECE, PcodeOp.CAST, PcodeOp.INDIRECT,
                      PcodeOp.MULTIEQUAL])
# Adding or masking an address still names the object plus an addend; scaling
# one does not, so a value that is multiplied or shifted is being used as a
# number even though the same bits could be an address.
ADDR_ARITH = frozenset([PcodeOp.INT_ADD, PcodeOp.INT_SUB, PcodeOp.INT_AND,
                        PcodeOp.INT_OR, PcodeOp.PTRADD, PcodeOp.PTRSUB])
SCALE_ARITH = frozenset([PcodeOp.INT_MULT, PcodeOp.INT_DIV, PcodeOp.INT_SDIV,
                         PcodeOp.INT_REM, PcodeOp.INT_SREM, PcodeOp.INT_LEFT,
                         PcodeOp.INT_RIGHT, PcodeOp.INT_SRIGHT,
                         PcodeOp.INT_XOR, PcodeOp.INT_NEGATE,
                         PcodeOp.INT_2COMP, PcodeOp.INT_MULT])
COMPARE = frozenset([PcodeOp.INT_EQUAL, PcodeOp.INT_NOTEQUAL,
                     PcodeOp.INT_LESS, PcodeOp.INT_SLESS,
                     PcodeOp.INT_LESSEQUAL, PcodeOp.INT_SLESSEQUAL,
                     PcodeOp.INT_CARRY, PcodeOp.INT_SCARRY, PcodeOp.INT_SBORROW])
BRANCH = frozenset([PcodeOp.BRANCHIND, PcodeOp.CALLIND, PcodeOp.CALL,
                    PcodeOp.BRANCH, PcodeOp.CBRANCH, PcodeOp.RETURN])

MAX_ROUNDS = 40

# A taint is either a pool word (keyed by its address) or a function's incoming
# argument register (keyed by entry point and index); both are seeded and
# followed by the same walk.
uses = {}
edges = {}
pc_sites = {}


def record(targets, name):
    for t in targets:
        uses.setdefault(t, {})
        uses[t][name] = uses[t].get(name, 0) + 1


def pass_on(targets, callee, index):
    for t in targets:
        edges.setdefault(t, set()).add((callee, index))


def seeded(op):
    """The pool address this op reads as a literal, or None.

    A pc-relative load is a LOAD from a constant address once the pc is folded;
    where the decoder folded it further the address shows up as a memory-space
    input instead, so both shapes are read.
    """
    if op.getOpcode() == PcodeOp.LOAD:
        off = op.getInput(1)
        if off.isConstant() and off.getOffset() in pool_targets:
            return off.getOffset()
        return None
    for i in range(op.getNumInputs()):
        vn = op.getInput(i)
        if vn.isAddress() and vn.getOffset() in pool_targets:
            return vn.getOffset()
    return None


def analyse(fn):
    entry = fn.getEntryPoint().getOffset()
    body = fn.getBody()
    instrs = []
    for instr in listing.getInstructions(body, True):
        instrs.append(instr)
    if not instrs:
        return
    index = {}
    for i, instr in enumerate(instrs):
        index[instr.getMinAddress().getOffset()] = i
    pcode = [instr.getPcode() for instr in instrs]
    # `ldr rN,[pc,#k]` followed by `add rN,pc` is the one place this image holds
    # a displacement rather than an address: the pool word is target minus the
    # adding instruction, so it is stale as soon as either end moves alone, and
    # it is neither a pointer nor a plain number.
    adds_pc = []
    for instr in instrs:
        raw = instr.getBytes()
        hw = (raw[0] & 0xFF) | ((raw[1] & 0xFF) << 8)
        adds_pc.append(all_regs[(hw & 7) | ((hw >> 4) & 8)]
                       if instr.getLength() == 2 and hw & 0xFF78 == 0x4478
                       else None)

    succ = []
    for i, instr in enumerate(instrs):
        nxt = []
        after = instr.getFallThrough()
        if after is not None and after.getOffset() in index:
            nxt.append(index[after.getOffset()])
        if instr.getFlowType().isJump():
            for to in instr.getFlows():
                j = index.get(to.getOffset())
                if j is not None:
                    nxt.append(j)
        succ.append(nxt)

    state = [None] * len(instrs)
    state[0] = dict((key, frozenset([("param", entry, a)]))
                    for a, key in enumerate(arg_keys))
    # Displacement from the address the taint started at, per varnode, and the
    # constant a varnode's value has been multiplied by; None where the walk
    # lost track. These are not merged at a join: a field offset is only
    # believed where one path produced it.
    disp, scale = {}, {}
    work = [0]
    rounds = 0
    while work and rounds < MAX_ROUNDS * len(instrs):
        rounds += 1
        i = work.pop()
        cur = state[i]
        if cur is None:
            continue
        cur = dict(cur)
        if adds_pc[i] is not None:
            site = instrs[i].getMinAddress().getOffset()
            got = cur.pop(adds_pc[i], None)
            if got:
                record(got, "pc_relative")
                for t in got:
                    pc_sites.setdefault(t, set()).add(site)
            # Past the add the register holds the sum, so what is done with it
            # from here on is about the target and says nothing about the word,
            # which holds only the distance.
        for op in pcode[i]:
            code = op.getOpcode()
            ins = [op.getInput(k) for k in range(op.getNumInputs())]
            tainted = []
            for k, vn in enumerate(ins):
                got = cur.get(vnkey(vn))
                if got:
                    tainted.append((k, got))
            if code == PcodeOp.LOAD or code == PcodeOp.STORE:
                base = 1
                for k, got in tainted:
                    if k == base:
                        record(got, "load_base" if code == PcodeOp.LOAD
                               else "store_base")
                        at = disp.get(vnkey(ins[base]))
                        if at is not None:
                            width = (op.getOutput().getSize()
                                     if code == PcodeOp.LOAD
                                     else ins[2].getSize())
                            record(got, "field%+d:%d" % (at, width))
                    elif k == 2:
                        record(got, "stored_value")
            elif code in BRANCH:
                for k, got in tainted:
                    if k == 0:
                        record(got, "call")
                    else:
                        record(got, "branch_condition")
                if code in (PcodeOp.CALL, PcodeOp.CALLIND):
                    for a, key in enumerate(arg_keys):
                        got = cur.get(key)
                        if not got:
                            continue
                        if code == PcodeOp.CALL and in_app(ins[0].getOffset()):
                            pass_on(got, ins[0].getOffset(), a)
                        else:
                            record(got, "argument_opaque")
                if code in (PcodeOp.CALL, PcodeOp.CALLIND):
                    for key in CLOBBERED:
                        if key in cur:
                            del cur[key]
                if code == PcodeOp.RETURN:
                    got = cur.get(arg_keys[0])
                    if got:
                        record(got, "returned")
            elif code in COMPARE:
                for _, got in tainted:
                    record(got, "compare")
            elif code in SCALE_ARITH:
                for _, got in tainted:
                    record(got, "scaled")
            elif code in ADDR_ARITH:
                for _, got in tainted:
                    record(got, "offset")
            elif tainted and code not in COPY_OPS:
                for _, got in tainted:
                    record(got, "other")

            out = op.getOutput()
            if out is None:
                continue
            key = vnkey(out)
            if code == PcodeOp.INT_MULT and any(v.isConstant() for v in ins):
                scale[key] = [v.getOffset() for v in ins if v.isConstant()][0]
            elif code == PcodeOp.INT_LEFT and ins[1].isConstant():
                scale[key] = 1 << ins[1].getOffset()
            elif code in COPY_OPS and ins[0:1] and vnkey(ins[0]) in scale:
                scale[key] = scale[vnkey(ins[0])]
            elif key in scale:
                del scale[key]
            seed = seeded(op)
            if seed is not None:
                cur[key] = frozenset([("pool", seed)])
                disp[key] = 0
                continue
            carried = set()
            if code in COPY_OPS or code in ADDR_ARITH:
                for _, got in tainted:
                    carried |= got
            if carried:
                cur[key] = frozenset(carried)
                here = disp.get(vnkey(ins[tainted[0][0]]))
                if code in COPY_OPS:
                    disp[key] = here
                elif code in (PcodeOp.INT_ADD, PcodeOp.INT_SUB):
                    other = ins[1 - tainted[0][0]] if len(ins) == 2 else None
                    sign = -1 if code == PcodeOp.INT_SUB else 1
                    if other is not None and other.isConstant() and here is not None:
                        disp[key] = here + sign * other.getOffset()
                    elif other is not None and vnkey(other) in scale:
                        # base + index * stride: the index's own multiplier is
                        # the stride, and this record is the one at index zero.
                        for _, got in tainted:
                            record(got, "stride:%d" % scale[vnkey(other)])
                        disp[key] = here
                    else:
                        disp[key] = None
                else:
                    disp[key] = None
            elif key in cur:
                del cur[key]
                disp.pop(key, None)

        for j in succ[i]:
            merged = cur if state[j] is None else None
            if merged is None:
                old = state[j]
                merged = dict(old)
                changed = False
                for k, v in cur.items():
                    if k not in merged:
                        merged[k] = v
                        changed = True
                    elif not v <= merged[k]:
                        merged[k] = merged[k] | v
                        changed = True
                if not changed:
                    continue
            state[j] = merged
            work.append(j)


done = 0
for fn in fm.getFunctions(app_set, True):
    analyse(fn)
    done += 1

# Close the call graph: what a callee does with its parameter is what the caller
# did with the value it passed. Only the use sets travel; a cycle stops growing
# on its own, so the sweep repeats until a pass adds nothing.
rounds = 0
changed = True
while changed:
    changed = False
    rounds += 1
    for taint in list(edges):
        got = uses.setdefault(taint, {})
        for callee, index in edges[taint]:
            for name, count in uses.get(("param", callee, index), {}).items():
                if name not in got:
                    got[name] = count
                    changed = True

rows = []
for target in sorted(pool_targets):
    got = uses.get(("pool", target), {})
    rows.append({"addr": target,
                 "uses": {k: got[k] for k in sorted(got)},
                 "pc_sites": sorted(pc_sites.get(("pool", target), ())),
                 "callees": sorted((c, i) for c, i
                                   in edges.get(("pool", target), ()))})

with open(os.path.join(out_dir, "word_uses.json"), "w") as fh:
    fh.write('{"_generated": %s,\n"functions": %d,\n"uses": ['
             % (json.dumps("by abi/ghidra/word_uses.py, do not edit: forward"
                           " p-code data flow from every pc-relative load to"
                           " every operation that consumes the loaded value"),
                done))
    for i, row in enumerate(rows):
        fh.write("%s\n%s" % ("," if i else "", json.dumps(row)))
    fh.write("]}\n")

println("word uses: %d rounds to close the call graph" % rounds)
println("word uses: %d functions, %d pool words, %d with a use"
        % (done, len(pool_targets), sum(1 for r in rows if r["uses"])))
