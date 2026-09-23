# Ghidra post-script: decompile every function and record what the decompiler
# could not reconcile with the prototypes it was given.
#
# Run last by abi/ghidra/analyze.sh; writes <out>/decompile_audit.json.
#
# The decompiler names a register it had to invent after where it came from:
# `in_rN` is read before anything in the function wrote it, so either the
# function takes more than its prototype says or a callee's prototype made it
# forward a register it never set; `extraout_rN` is a register a call left
# behind that the caller goes on to use, so the callee returns more than its
# prototype says, or the caller's own prototype claims a register the callee
# clobbered. Each is attributed to the call it flows into or out of, which is
# where the missing or wrong prototype usually is.
# @category HWA10

import collections
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor

from ghidra.app.decompiler import DecompInterface, DecompileOptions
from ghidra.program.model.pcode import PcodeOp

TIMEOUT_S = 60
# Values the decompiler follows an invented input through before calling a use
# the input's own: copies, casts, joins and concatenations do not consume it.
PASS_THROUGH = (PcodeOp.COPY, PcodeOp.CAST, PcodeOp.MULTIEQUAL, PcodeOp.INDIRECT,
                PcodeOp.PIECE)
# The VFP status and the APSR flags are read by `vmrs`/`vcmp` sequences Ghidra
# models as inputs; they say nothing about a prototype.
FLAG_INPUTS = re.compile(r"^in_(fpscr|CY|ZR|NG|OV)")

listing = currentProgram.getListing()
local = threading.local()


def decompiler():
    if not hasattr(local, "ifc"):
        ifc = DecompInterface()
        ifc.setOptions(DecompileOptions())
        ifc.openProgram(currentProgram)
        local.ifc = ifc
    return local.ifc


def call_target(at):
    """The callee of the call instruction at `at`: an address, or "indirect"."""
    ins = listing.getInstructionAt(at)
    if ins is None or not ins.getFlowType().isCall() and not ins.getFlowType().isJump():
        return None
    for ref in ins.getReferencesFrom():
        if ref.getReferenceType().isCall() or ref.getReferenceType().isJump():
            return int(ref.getToAddress().getOffset())
    return "indirect"


def only_pushed(fn, reg):
    """True where the prologue's push is the only read of `reg` before it is written.

    GCC reserves stack by pushing argument registers it does not care about
    (`push {r0-r4, lr}` in newlib's cos makes room for y[2]), and the
    decompiler, which cannot see the slot filled through the pointer a callee is
    given, carries the pushed register to the slot's first use as an input. The
    machine code settles it: along the fall-through path from the push, the
    register is either written before anything reads it or it is a real input.
    A branch or call reached first leaves it an input.
    """
    ins = listing.getInstructionAt(fn.getEntryPoint())
    if ins is None or ins.getMnemonicString().lower() not in ("push", "stmdb"):
        return False
    register = currentProgram.getRegister(reg)
    if register is None or register not in ins.getInputObjects():
        return False
    ins = ins.getNext()
    while ins is not None and fn.getBody().contains(ins.getAddress()):
        if register in ins.getInputObjects():
            return False
        if register in ins.getResultObjects():
            return True
        if not ins.getFlowType().isFallthrough():
            return False
        ins = ins.getNext()
    return False


def consumers(vn, seen=None):
    """Where an invented input ends up: the calls it is an argument of, else the ops."""
    seen = set() if seen is None else seen
    out = []
    for op in vn.getDescendants():
        if op.getSeqnum() in seen:
            continue
        seen.add(op.getSeqnum())
        opc = op.getOpcode()
        if opc == PcodeOp.CALL:
            out.append(int(op.getInput(0).getAddress().getOffset()))
        elif opc == PcodeOp.CALLIND:
            out.append("indirect")
        elif opc in PASS_THROUGH and op.getOutput() is not None:
            out += consumers(op.getOutput(), seen)
        else:
            out.append(op.getMnemonic())
    return out


def audit(fn):
    rec = {"address": int(fn.getEntryPoint().getOffset()), "name": fn.getName(),
           "prototype": str(fn.getSignatureSource()), "warnings": [], "in": [],
           "extraout": [], "unaff": [], "pushed": []}
    res = decompiler().decompileFunction(fn, TIMEOUT_S, monitor)
    if not res.decompileCompleted():
        rec["failed"] = str(res.getErrorMessage()).strip()
        return rec
    text = res.getDecompiledFunction().getC()
    rec["warnings"] = sorted({re.sub(r"0x[0-9a-f]+|\b[0-9a-f]{5,}\b", "N", w)
                              for w in re.findall(r"WARNING: ([^*\n]+?)\s*\*/", text)})
    for sym in res.getHighFunction().getLocalSymbolMap().getSymbols():
        name, hv = sym.getName(), sym.getHighVariable()
        if hv is None:
            continue
        reg = re.sub(r"_[0-9a-f]+$", "", name)
        if name.startswith("in_"):
            if FLAG_INPUTS.match(name):
                continue
            if only_pushed(fn, reg[len("in_"):]):
                rec["pushed"].append(reg)
                continue
            into = set()
            for vn in hv.getInstances():
                if vn.isInput():
                    into.update(consumers(vn))
            rec["in"].append({"var": reg, "into": sorted(into, key=str)})
        elif name.startswith("extraout_"):
            out_of = {call_target(vn.getDef().getSeqnum().getTarget())
                      for vn in hv.getInstances() if vn.getDef() is not None}
            rec["extraout"].append({"var": reg, "from": sorted(out_of - {None}, key=str)})
        elif name.startswith("unaff_"):
            rec["unaff"].append(reg)
    return rec


def summarise(records):
    by_addr = {r["address"]: r["name"] for r in records}
    label = lambda t: by_addr.get(t, t if isinstance(t, str) else "0x%x" % t)
    callees = collections.Counter()
    for r in records:
        for v in r["extraout"]:
            callees.update(label(t) for t in v["from"])
        for v in r["in"]:
            callees.update(label(t) for t in v["into"] if not isinstance(t, str) or t == "indirect")
    flagged = [r for r in records if r["in"] or r["extraout"] or r["unaff"] or r["warnings"]]
    return {
        "functions": len(records),
        "with_prototype": sum(r["prototype"] != "DEFAULT" for r in records),
        "flagged": len(flagged),
        "with_in": sum(bool(r["in"]) for r in records),
        "with_extraout": sum(bool(r["extraout"]) for r in records),
        "with_unaff": sum(bool(r["unaff"]) for r in records),
        "with_pushed": sum(bool(r["pushed"]) for r in records),
        "failed": sum("failed" in r for r in records),
        "in_vars": dict(collections.Counter(v["var"] for r in records for v in r["in"]).most_common()),
        "extraout_vars": dict(collections.Counter(v["var"] for r in records for v in r["extraout"]).most_common()),
        "warnings": dict(collections.Counter(w for r in records for w in r["warnings"]).most_common()),
        "callees": dict(callees.most_common(60)),
    }


args = getScriptArgs()
if len(args) != 1:
    raise RuntimeError("audit_decompile.py takes the output directory")
functions = list(currentProgram.getFunctionManager().getFunctions(True))
# Half the cores: the audit runs inside analyze.sh on the machine being worked at.
with ThreadPoolExecutor(max(1, os.cpu_count() // 2)) as pool:
    records = list(pool.map(audit, functions))
totals = summarise(records)
with open(os.path.join(args[0], "decompile_audit.json"), "w") as fh:
    json.dump({"totals": totals, "functions": records}, fh, indent=1)
println("decompile audit: %d functions, %d with a prototype, %d flagged: in_ %d, extraout_ %d,"
        " unaff_ %d, failed %d; %d push argument registers no parameter covers"
        % (totals["functions"], totals["with_prototype"], totals["flagged"], totals["with_in"],
           totals["with_extraout"], totals["with_unaff"], totals["failed"], totals["with_pushed"]))
