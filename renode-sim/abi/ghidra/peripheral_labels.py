#!/usr/bin/env python3
"""Put the chip's register map into the Ghidra project as blocks and labels.

    ~/ref-build/ghidra/venv/bin/python abi/ghidra/peripheral_labels.py [--project DIR]

abi/ghidra/seed_symbols.py creates the uninitialised blocks the analysis needs
to classify a reference as external, and stops there: every peripheral address
in the listing is a bare number, so `ldr r3,=0x40007000` says nothing and the
reader has to go and look it up. abi/peripherals.py already has the names out
of NRF52840.svd, so this writes them in -- one label per peripheral base and
one per register, each with the SVD's own description as its comment.

It is a sibling of seed_symbols.py rather than part of it because seed_symbols
runs inside analyzeHeadless and the register map is not evidence the analysis
uses: a label on an address nothing in the program's bytes covers changes no
decision the analyser makes, so it does not have to be there before the
analysis and re-running abi/ghidra/analyze.sh is too expensive to ask for a
name. Run it the way mark_review.py is run, against the project that exists;
the Ghidra GUI holds the project open, so it has to be closed first.
"""

import argparse
import glob
import os
import sys

import pyghidra

HERE = os.path.dirname(os.path.abspath(__file__))
ABI = os.path.dirname(HERE)
# Loaded by path rather than by putting abi/ on sys.path: this file's own
# directory is abi/ghidra/, and with abi/ first on the path `import ghidra`
# resolves to that directory instead of pyghidra's package, which sends the
# launcher into an import recursion before Ghidra starts.
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location(
    "peripherals", os.path.join(ABI, "peripherals.py"))
peripherals = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(peripherals)

ROOT = os.environ.get("ROOT", os.path.expanduser("~/ref-build"))

# The blocks the register map needs to hang a label on. seed_symbols.py already
# creates the two big ones; the information pages are outside both of them.
BLOCKS = [("ficr_uicr", 0x10000000, 0x2000)]


def label_name(location):
    """A Ghidra label for a Location.

    Ghidra takes a `.` in a symbol but not a `[`, and the SVD spells its arrays
    with both, so an indexed register comes out flattened: `CH[0].PSELP` is
    `NRF_SAADC_CH0_PSELP`. The arrow form the rest of the repo prints stays in
    the comment, which is where a reader looks it up.
    """
    if location.register is None:
        return "NRF_%s" % location.peripheral.name
    spelled = (location.register.name.replace("[", "").replace("]", "")
               .replace(".", "_"))
    return "NRF_%s_%s" % (location.peripheral.name, spelled)


def addresses(chip):
    """Every address the map names, resolved once, in address order.

    A base two peripherals share is resolved by abi/words.yaml or it is not
    resolved at all; an address the declaration does not settle is left without
    a label rather than given one of the two names.
    """
    wanted = set()
    for p in chip.peripherals:
        wanted.add(p.base)
        for reg in p.registers:
            wanted.add(p.base + reg.offset)
    out, refused = [], []
    for at in sorted(wanted):
        try:
            found = chip.at(at)
        except peripherals.Ambiguous as why:
            refused.append(str(why))
            continue
        if found is not None:
            out.append(found)
    return out, refused


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=os.path.join(ROOT, "ghidra-project"))
    ap.add_argument("--program", default="appl.bin")
    ap.add_argument("--install", default=None)
    args = ap.parse_args()

    chip = peripherals.load()
    located, refused = addresses(chip)

    install = args.install
    if install is None:
        found = sorted(glob.glob(os.path.join(ROOT, "ghidra", "ghidra_*")))
        if not found:
            raise SystemExit("no Ghidra under %s; run abi/ghidra/fetch.sh"
                             % os.path.join(ROOT, "ghidra"))
        install = found[-1]
    pyghidra.start(install_dir=install)
    from ghidra.program.model.listing import CodeUnit
    from ghidra.program.model.symbol import SourceType
    from ghidra.util.task import TaskMonitor
    from java.lang import Object as JObject

    project = pyghidra.open_project(args.project, "hwa10", create=False)
    made = {"blocks": 0, "labels": 0}
    try:
        domain_file = project.getProjectData().getRootFolder().getFile(args.program)
        if domain_file is None:
            raise SystemExit("no %s in the project %s"
                             % (args.program, args.project))
        consumer = JObject()
        program = domain_file.getDomainObject(consumer, False, False,
                                              TaskMonitor.DUMMY)
        space = program.getAddressFactory().getDefaultAddressSpace()
        memory = program.getMemory()
        listing = program.getListing()
        table = program.getSymbolTable()
        tx = program.startTransaction("peripheral register map")
        try:
            for name, base, size in BLOCKS:
                if memory.getBlock(space.getAddress(base)) is not None:
                    continue
                block = memory.createUninitializedBlock(
                    name, space.getAddress(base), size, False)
                block.setRead(True)
                block.setWrite(True)
                made["blocks"] += 1
            for found in located:
                at = space.getAddress(found.address)
                if memory.getBlock(at) is None:
                    continue
                name = label_name(found)
                if any(s.getName() == name for s in table.getSymbols(at)):
                    continue
                table.createLabel(at, name, SourceType.USER_DEFINED)
                note = found.name
                if found.register is not None:
                    note = "%s (%s)%s" % (
                        found.name, found.register.access,
                        "\n" + found.register.description
                        if found.register.description else "")
                listing.setComment(at, CodeUnit.PLATE_COMMENT, note)
                made["labels"] += 1
        finally:
            program.endTransaction(tx, True)
        program.save("peripheral register map", TaskMonitor.DUMMY)
        program.release(consumer)
    finally:
        project.close()
    print("created %(blocks)d memory blocks and %(labels)d register labels"
          % made)
    for why in refused:
        print("  not labelled: %s" % why)
    return 0


if __name__ == "__main__":
    sys.exit(main())
