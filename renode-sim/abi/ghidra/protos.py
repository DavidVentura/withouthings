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
  newlib     the map's libc functions as newlib's installed headers declare
             them, for the bodies newlib writes in assembly (memchr, strlen)
             and the syscall stubs Withings supplies, whose DWARF has no types
  s140       the SoftDevice's SVC wrappers, which the S140 headers define
             themselves: SVCALL is a naked static function under GCC, so
             naming each one is enough to have it emitted

<out>/protos.json lists each source's object, the functions it defines and the
ones declared with the base procedure call standard (`pcs("aapcs")`), which
DWARF does not record; abi/ghidra/seed.py routes every seeded function to one
of them.
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
NEWLIB_INCLUDE = os.path.join(os.path.dirname(TOOLBIN), "arm-none-eabi", "include")
SDK = os.path.join(ROOT, "sdk", "nRF5_SDK_17.1.0_ddde560")
S140_HEADERS = os.path.join(SDK, "components", "softdevice", "s140", "headers")
# nrf_sdm.h includes the MDK's nrf.h, which wants the part and CMSIS; the
# MBR's one SVC call, sd_mbr_command, is declared beside the MBR.
S140_FLAGS = ["-DNRF52840_XXAA", "-I", S140_HEADERS, "-I", os.path.join(S140_HEADERS, "nrf52"),
              "-I", os.path.join(SDK, "components", "softdevice", "mbr", "headers"),
              "-I", os.path.join(SDK, "modules", "nrfx", "mdk"),
              "-I", os.path.join(SDK, "components", "toolchain", "cmsis", "include")]
# `NC` is a prototype declaration; `NF` is a definition, the headers' static
# inlines, which the image has no body of its own for.
AUX_DECL = re.compile(r"/\* (\S+):(\d+):NC \*/ (.*)$")
# The classes whose name is the library's own, so the library's DWARF or its
# headers are the declaration and a hand header is at most a copy of it.
LIBC_CLASSES = ("libc", "libm", "syscall")
# Every newlib header a libc, libm or syscall body of the image is declared in.
NEWLIB_HEADERS = ["string.h", "strings.h", "stdlib.h", "stdio.h", "math.h", "time.h",
                  "ctype.h", "locale.h", "wchar.h", "signal.h", "reent.h", "unistd.h",
                  "sys/unistd.h", "sys/stat.h", "assert.h", "setjmp.h"]
# Every S140 header that declares SVC wrappers. nrf_nvic.h is not one: its
# sd_nvic_* are ordinary inline C around the NVIC.
S140_SVC_HEADERS = ["nrf_sdm.h", "nrf_soc.h", "ble.h", "ble_gap.h", "ble_gatts.h",
                    "ble_gattc.h", "ble_l2cap.h", "nrf_mbr.h"]
# GAS labels its units with this, whatever the target.
DW_LANG_MIPS_ASSEMBLER = 0x8001
BASE_PCS = re.compile(r'__attribute__\s*\(\(\s*pcs\s*\(\s*"aapcs"\s*\)\s*\)\)[^;{]*?\b(\w+)\s*\(')


def declaration_at(lines, first):
    """The declaration starting at line `first` (0-based), up to its `;`."""
    text = ""
    for line in lines[first:]:
        text += line + "\n"
        if ";" in line:
            return text.strip()
    raise ValueError("declaration at line %d has no ';'" % (first + 1))


def definition_of(decl, name):
    """An empty definition with the declaration's own text.

    Attributes after the parameter list (newlib's `_ATTRIBUTE ((__noreturn__))`
    on _exit) are not allowed there in a definition, so they move in front.
    """
    if decl.startswith("extern "):
        decl = decl[len("extern "):]
    m = re.search(r"\b%s\s*\(" % re.escape(name), decl)
    if m is None:
        raise ValueError("%s is not declared in: %s" % (name, decl))
    depth, end = 0, None
    for i in range(m.end() - 1, len(decl)):
        depth += {"(": 1, ")": -1}.get(decl[i], 0)
        if depth == 0:
            end = i
            break
    if end is None:
        raise ValueError("%s: unbalanced parameter list in: %s" % (name, decl))
    tail = decl[end + 1:].rstrip().rstrip(";").strip()
    return ((tail + " ") if tail else "") + decl[:end + 1] + " {}"


def stubs(aux, read_header, keep, names=None):
    """Empty definitions of the declarations GCC's -aux-info lists.

    `aux` is the listing; `read_header` maps the header path it names to its
    lines; `keep` says which headers count; `names`, where given, which
    functions. The definition is the declaration's own text, and the TU
    includes the header above it, so GCC rejects any definition that does not
    match what the header declares. A name declared twice is defined once.
    """
    out, seen = [], set()
    for entry in aux.splitlines():
        m = AUX_DECL.match(entry)
        if m is None or not keep(m.group(1)):
            continue
        name = re.search(r"(\w+)\s*\(", m.group(3)).group(1)
        if name in seen or (names is not None and name not in names):
            continue
        seen.add(name)
        out.append(definition_of(declaration_at(read_header(m.group(1)),
                                                int(m.group(2)) - 1), name))
    return out


def read_lines(path):
    with open(path) as fh:
        return fh.read().splitlines()


def gcc(args, std="gnu99"):
    subprocess.check_call([shapes.GCC] + [a for a in shapes.ARCH if not a.startswith("-std=")]
                          + ["-std=" + std, "-w"] + args)


def compile_stubs(out_dir, name, includes, keep, names=None, std="gnu99", flags=()):
    """<name>.o: the empty definitions of what `includes` declares."""
    head = "".join("#include %s\n" % inc for inc in includes)
    probe = os.path.join(out_dir, name + "-probe.c")
    aux = os.path.join(out_dir, name + ".aux")
    with open(probe, "w") as fh:
        fh.write(head)
    gcc(list(flags) + ["-fsyntax-only", "-aux-info", aux, probe], std)
    with open(aux) as fh:
        defs = stubs(fh.read(), read_lines, keep, names)
    src = os.path.join(out_dir, name + ".c")
    with open(src, "w") as fh:
        fh.write(head + "\n".join(defs) + "\n")
    obj = os.path.join(out_dir, name + ".o")
    gcc(list(flags) + ["-g", "-O0", "-fno-builtin", "-c", src, "-o", obj], std)
    return obj, base_pcs(src, flags, std)


def base_pcs(src, flags, std):
    """The functions `src` declares with pcs("aapcs"), read after preprocessing,
    so an attribute behind a macro counts."""
    text = subprocess.check_output([shapes.GCC] + [a for a in shapes.ARCH if not a.startswith("-std=")]
                                   + ["-std=" + std, "-w", "-E", "-P"] + list(flags) + [src],
                                   text=True)
    return sorted(set(BASE_PCS.findall(text)))


def compile_hand(out_dir):
    return compile_stubs(out_dir, "hand", ['"withings/all.h"'],
                         lambda path: "/include/withings/" in path,
                         flags=["-I", shapes.INCLUDE])


def compile_newlib(out_dir, names):
    """newlib's own declarations of `names`. Its headers leave most parameters
    unnamed, which a definition may only do from C2x on, and declare the
    syscall stubs (_write, _isatty, _kill) only to newlib's own build, which
    defines _LIBC."""
    return compile_stubs(out_dir, "newlib", ["<%s>" % h for h in NEWLIB_HEADERS],
                         lambda path: path.startswith(NEWLIB_INCLUDE + "/"),
                         names=names, std="gnu2x", flags=["-D_LIBC"])


def compile_s140(out_dir, names):
    """The SVC wrappers `names`, as the S140 headers define them."""
    src = os.path.join(out_dir, "s140.c")
    with open(src, "w") as fh:
        fh.write("".join('#include "%s"\n' % h for h in S140_SVC_HEADERS))
        fh.write("void *const s140_wrappers[] = {\n%s};\n"
                 % "".join("    (void *)%s,\n" % n for n in sorted(names)))
    obj = os.path.join(out_dir, "s140.o")
    gcc(S140_FLAGS + ["-g", "-O0", "-c", src, "-o", obj])
    return obj, []


def link_libc(names, out_dir):
    """The libc.a members defining `names`, as one relocatable object.

    --whole-archive would collide on the members that define the same symbol for
    different configurations; `-u` pulls exactly the members the map needs and
    what they call. A static function (mktime.c's validate_structure) has no
    symbol `-u` can name, so its member is pulled by a global it defines.
    """
    archive = os.path.join(NEWLIB, "libc.a")
    listing = subprocess.check_output(
        [os.path.join(TOOLBIN, "arm-none-eabi-nm"), "-A", "--defined-only", archive], text=True)
    members, local = {}, {}
    for line in listing.splitlines():
        where, _, kind, sym = (line.rsplit(":", 1)[0],) + tuple(line.rsplit(":", 1)[1].split())
        if kind in "TW":
            members.setdefault(where, sym)
        elif kind == "t":
            local.setdefault(sym, where)
    wanted = set(names)
    for name in names:
        if name in local and local[name] in members:
            wanted.add(members[local[name]])
    obj = os.path.join(out_dir, "libc.o")
    cmd = [os.path.join(TOOLBIN, "arm-none-eabi-ld"), "-r", "-o", obj]
    for name in sorted(wanted):
        cmd += ["-u", name]
    subprocess.check_call(cmd + [archive])
    return obj


def dwarf_functions(path):
    """The functions the object's DWARF gives a body, which are the ones Ghidra
    imports with a signature: a symbol alone, like newlib's assembly memchr,
    declares nothing, and a body inlined everywhere leaves no function to read.

    An assembler unit's subprograms are GAS's, which know a name and an address
    and no types, so they declare nothing either. An out-of-line copy of an
    inline function names itself through its abstract origin. The debug sections
    are read unrelocated: a relocatable object's string offsets are REL addends
    already in place, and the executables have none.
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
    app_functions = [s for s in smap.symbols if s.kind == "function" and s.name
                     and S.APP_BASE <= s.address < S.APP_END]
    libc_names = {s.declared_name for s in app_functions if s.klass in LIBC_CLASSES}
    svc_names = {s.declared_name for s in app_functions if s.klass == "svc"}

    objects, softfp = {}, {}
    objects["hand"], softfp["hand"] = compile_hand(out_dir)
    for v in variants:
        ref = os.path.join(ROOT, "build", v, "ref.elf")
        if not os.path.exists(ref):
            sys.exit("abi/ghidra/protos.py: %s is missing; run abi/refbuild.sh" % ref)
        objects[v] = link_into(out_dir, v + ".elf", ref)
    objects["libc"] = link_libc(libc_names, out_dir)
    objects["newlib"], softfp["newlib"] = compile_newlib(out_dir, libc_names)
    objects["s140"], softfp["s140"] = compile_s140(out_dir, svc_names)

    sources = [{"name": name, "file": os.path.basename(path),
                "defines": dwarf_functions(path), "base_pcs": softfp.get(name, [])}
               for name, path in objects.items()]
    with open(os.path.join(args.out, "protos.json"), "w") as fh:
        json.dump({"sources": sources}, fh, indent=1)
    for s in sources:
        print("protos: %-10s %-14s %5d functions, %d base pcs"
              % (s["name"], s["file"], len(s["defines"]), len(s["base_pcs"])), file=sys.stderr)


if __name__ == "__main__":
    main()
