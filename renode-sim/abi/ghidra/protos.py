#!/usr/bin/env python3
"""Build the objects whose DWARF carries the function prototypes Ghidra applies.

    python3 abi/ghidra/protos.py [--out abi/out/ghidra]

abi/ghidra/analyze.sh runs this before abi/ghidra/seed.py. Each object is one
prototype source, and Ghidra's own DWARF importer reads it, so what a parameter
is comes from the compiler that read the declaration and not from a second C
parser of ours:

  hand       every function abi/include/withings declares, compiled as an empty
             definition, since GCC gives a bare declaration's parameters a type
             but no name
  <variant>  the reference builds abi/matches.yaml measured against
  libc       the members of the debug-built newlib that define the map's libc
             functions, which the archive only yields linked out one by one

<out>/protos.json lists each source's object and the functions it defines;
abi/ghidra/seed.py routes every seeded function to one of them.
"""

import argparse
import json
import os
import re
import subprocess
import sys

import yaml
from elftools.elf.elffile import ELFFile

HERE = os.path.dirname(os.path.abspath(__file__))
ABI = os.path.dirname(HERE)
sys.path.insert(0, ABI)

import shapes            # noqa: E402
import symbols as S      # noqa: E402

ROOT = os.environ.get("REF_BUILD", os.path.expanduser("~/ref-build"))
TOOLBIN = os.path.dirname(shapes.GCC)
NEWLIB = os.path.join(ROOT, "build", "newlib-nano-ll", "arm-none-eabi", "newlib")
# `NC` is a prototype declaration; `NF` is a definition, the headers' static
# inlines, which the image has no body of its own for.
AUX_DECL = re.compile(r"/\* (\S+/include/withings/[^:\s]+):(\d+):NC \*/")
# The classes whose name is the reference's own, so the reference's DWARF is
# the declaration and a hand header is at most a copy of it.
LIBC_CLASSES = ("libc", "syscall")
# GAS labels its units with this, whatever the target.
DW_LANG_MIPS_ASSEMBLER = 0x8001


def declaration_at(lines, first):
    """The declaration starting at line `first` (0-based), up to its `;`."""
    text = ""
    for line in lines[first:]:
        text += line + "\n"
        if ";" in line:
            return text.strip()
    raise ValueError("declaration at line %d has no ';'" % (first + 1))


def hand_stubs(aux, read_header):
    """The translation unit that defines, empty, every function the headers declare.

    `aux` is GCC's -aux-info listing of the umbrella header; `read_header` maps
    the header path it names to the header's lines. The definition is the
    declaration's own text, so it cannot drift from it, and the header is
    included above it, so GCC rejects any definition that does not match its
    declaration.
    """
    out = ['#include "withings/all.h"']
    for entry in aux.splitlines():
        m = AUX_DECL.match(entry)
        if m is None:
            continue
        decl = declaration_at(read_header(m.group(1)), int(m.group(2)) - 1)
        if decl.startswith("extern "):
            decl = decl[len("extern "):]
        out.append(decl[:-1].rstrip() + " {}")
    return "\n".join(out) + "\n"


def compile_hand(out_dir):
    probe = os.path.join(out_dir, "hand-probe.c")
    aux = os.path.join(out_dir, "hand.aux")
    with open(probe, "w") as fh:
        fh.write('#include "withings/all.h"\n')
    subprocess.check_call([shapes.GCC] + shapes.ARCH + [
        "-w", "-I", shapes.INCLUDE, "-fsyntax-only", "-aux-info", aux, probe])

    def read_header(path):
        with open(path) as fh:
            return fh.read().splitlines()

    stubs = os.path.join(out_dir, "hand.c")
    with open(aux) as fh:
        text = hand_stubs(fh.read(), read_header)
    with open(stubs, "w") as fh:
        fh.write(text)
    obj = os.path.join(out_dir, "hand.o")
    subprocess.check_call([shapes.GCC] + shapes.ARCH + [
        "-g", "-O0", "-w", "-I", shapes.INCLUDE, "-c", stubs, "-o", obj])
    return obj


def link_libc(names, out_dir):
    """The libc.a members defining `names`, as one relocatable object.

    --whole-archive would collide on the members that define the same symbol for
    different configurations; `-u` pulls exactly the members the map needs and
    what they call.
    """
    obj = os.path.join(out_dir, "libc.o")
    cmd = [os.path.join(TOOLBIN, "arm-none-eabi-ld"), "-r", "-o", obj]
    for name in sorted(names):
        cmd += ["-u", name]
    subprocess.check_call(cmd + [os.path.join(NEWLIB, "libc.a")])
    return obj


def dwarf_functions(path):
    """The functions the object's DWARF gives a body, which are the ones Ghidra
    imports with a signature: a symbol alone, like newlib's assembly memchr,
    declares nothing, and a body inlined everywhere leaves no function to read.

    An assembler unit's subprograms are GAS's, which know a name and an address
    and no types, so they declare nothing either. An out-of-line copy of an
    inline function names itself through its abstract origin. The debug sections are read unrelocated: a relocatable object's
    string offsets are REL addends already in place, and the executables have
    none.
    """
    names = set()
    with open(path, "rb") as fh:
        dwarf = ELFFile(fh).get_dwarf_info(relocate_dwarf_sections=False)
        for cu in dwarf.iter_CUs():
            top = cu.get_top_DIE()
            if top.attributes["DW_AT_language"].value == DW_LANG_MIPS_ASSEMBLER:
                continue
            for die in top.iter_children():
                if die.tag != "DW_TAG_subprogram" or "DW_AT_low_pc" not in die.attributes:
                    continue
                if "DW_AT_abstract_origin" in die.attributes:
                    die = die.get_DIE_from_attribute("DW_AT_abstract_origin")
                if "DW_AT_name" in die.attributes:
                    names.add(die.attributes["DW_AT_name"].value.decode())
    return sorted(names)


def link_into(out_dir, name, target):
    """A reference object under the name Ghidra will import it as."""
    link = os.path.join(out_dir, name)
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(target, link)
    return link


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ABI, "out", "ghidra"))
    args = ap.parse_args()
    out_dir = os.path.join(args.out, "protos")
    os.makedirs(out_dir, exist_ok=True)

    with open(os.path.join(ABI, "matches.yaml")) as fh:
        variants = yaml.safe_load(fh)["meta"]["variants"]
    smap = S.load()
    libc_names = {s.name for s in smap.symbols
                  if s.kind == "function" and s.name and s.klass in LIBC_CLASSES
                  and S.APP_BASE <= s.address < S.APP_END}

    objects = {"hand": compile_hand(out_dir)}
    for v in variants:
        ref = os.path.join(ROOT, "build", v, "ref.elf")
        if not os.path.exists(ref):
            sys.exit("abi/ghidra/protos.py: %s is missing; run abi/refbuild.sh" % ref)
        objects[v] = link_into(out_dir, v + ".elf", ref)
    objects["libc"] = link_libc(libc_names, out_dir)

    sources = [{"name": name, "file": os.path.basename(path),
                "defines": dwarf_functions(path)}
               for name, path in objects.items()]
    with open(os.path.join(args.out, "protos.json"), "w") as fh:
        json.dump({"sources": sources}, fh, indent=1)
    for s in sources:
        print("protos: %-10s %-14s %5d functions" % (s["name"], s["file"], len(s["defines"])),
              file=sys.stderr)


if __name__ == "__main__":
    main()
