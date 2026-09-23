# Ghidra pre-script for the prototype objects abi/ghidra/protos.py builds.
#
# Run by abi/ghidra/analyze.sh on each object it imports into the project's
# /protos folder; not useful standalone. Those programs are read for their
# DWARF and nothing else, so every analyzer but the DWARF importer is off: the
# reference ELFs are whole builds, and disassembling them would only cost time.
# @category HWA10

opts = currentProgram.getOptions("Analyzers")
for name in opts.getOptionNames():
    if "." not in name and opts.getType(name).name() == "BOOLEAN_TYPE":
        opts.setBoolean(name, name == "DWARF")
