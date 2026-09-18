#!/usr/bin/env python3
"""The shapes the hand headers declare, read out of the compiler.

    python3 abi/shapes.py                 # what the headers declare, as a report

abi/include/withings/*.h is C, so the authority on what a struct's fields are,
where they sit, how wide they are and which of them are pointers is the
compiler that reads them -- not a second type engine of ours that has to agree
with it. This compiles the headers once with the toolchain the rest of the
build uses and reads the answer back out of the DWARF.

Everything downstream asks the same object: abi/blobify.py and abi/datagen.py
for the initialiser a declared table becomes, abi/runs.py for which words of a
row are pointers, abi/reach.py for which column of a dispatch row holds the
handler, abi/ghidra/seed.py for the struct to lay over the rows.

A table is `extern struct row_t name[N];` in a header and one entry in
abi/symbols.yaml, so its stride is `sizeof(struct row_t)` by construction and
cannot disagree with its declaration. What can still disagree is the image, and
abi/blobify.py is what checks that: the section the partition cut has to be
exactly `N * sizeof(row)` bytes long.
"""

import os
import re
import subprocess
import sys

from elftools.elf.elffile import ELFFile

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
INCLUDE = os.path.join(HERE, "include")
UMBRELLA = os.path.join(INCLUDE, "withings", "all.h")

# The SDK's own compiler, which is the one the app was built with and the one
# abi/relink.sh compiles the replacement bodies with; a layout is only the
# image's layout if the compiler that reads it is the same one.
GCC = os.path.join(os.environ.get("REF_BUILD", os.path.expanduser("~/ref-build")),
                   "gcc-arm-none-eabi-9-2020-q2-update", "bin", "arm-none-eabi-gcc")
ARCH = ["-mcpu=cortex-m4", "-mthumb", "-mabi=aapcs", "-mfpu=fpv4-sp-d16",
        "-mfloat-abi=hard", "-fshort-enums", "-std=gnu99"]

# Pointer, scalar and array are the three shapes anything downstream cares
# about: a word the linker has to relocate, a number read out of the image, and
# a run of either.
POINTER, SCALAR, ARRAY, STRUCT = "pointer", "scalar", "array", "struct"


class Field(object):
    """One member of a struct, at the offset the compiler put it."""

    def __init__(self, name, offset, size, kind, ctype, count=1, unit=0,
                 points_to=None):
        self.name, self.offset, self.size = name, offset, size
        self.kind, self.ctype, self.count = kind, ctype, count
        self.unit = unit or size
        # What a pointer field points at: "code" for a function pointer,
        # "char" for a string, "void" where the target has no type, "data" for
        # anything else. It is what tells a dispatch row's handler column from
        # its name column, and a bitmap column from either.
        self.points_to = points_to

    def __repr__(self):
        return "<%s %s +0x%x %d>" % (self.kind, self.name, self.offset, self.size)


class Struct(object):
    def __init__(self, name, size, fields):
        self.name, self.size, self.fields = name, size, fields

    def field(self, name):
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError("%s has no field %s" % (self.name, name))

    def pointer_map(self):
        """{offset: is this word a pointer} over the whole row."""
        return dict((f.offset, f.kind == POINTER) for f in self.fields)


class Object(object):
    """An `extern` declaration: a global, or a table with its element and count.

    `element` is the row struct where there is one and None otherwise, and
    `kind` is what one element is -- a pointer word, a scalar or a struct --
    which is what says whether the bytes at the address are a relocation site.
    """

    def __init__(self, name, ctype, size, element=None, count=1, kind=SCALAR,
                 points_to=None):
        self.name, self.ctype, self.size = name, ctype, size
        self.element, self.count = element, count
        self.kind, self.points_to = kind, points_to

    @property
    def stride(self):
        return self.size // self.count if self.count else self.size


class Table(object):
    """A table: one entry in the address map and one declaration in a header.

    The stride is `sizeof` the row struct, so a table cannot be declared with a
    stride that disagrees with its own rows. What can still disagree is the
    image, and abi/blobify.py is what checks that: the section the partition cut
    at the table's address has to be exactly `count * stride` bytes long.
    """

    def __init__(self, symbol, obj, row):
        self.name, self.address = symbol.name, symbol.address
        self.entry, self.row = obj.element, row
        self.count, self.stride = obj.count, row.size
        self.end = self.address + self.stride * self.count

    def rows(self):
        return range(self.address, self.end, self.stride)

    def pointer_map(self):
        return self.row.pointer_map()

    def __repr__(self):
        return "<%s[%d] @0x%x>" % (self.entry, self.count, self.address)


class Types(object):
    def __init__(self, structs, objects, enums, names):
        self.structs, self.objects, self.enums = structs, objects, enums
        # Every identifier the headers declare, which is what a generated
        # source needs in order not to declare one of them a second time with a
        # shape that would not compile beside the real one.
        self.declared = names

    def struct(self, name):
        return self.structs[name]

    def object(self, name):
        return self.objects[name]

    def tables(self, smap):
        """Every declared table, joined to the address the map gives it."""
        out = []
        for sym in smap.of_kind("table"):
            obj = self.object(sym.name)
            out.append(Table(sym, obj, self.struct(obj.element)))
        return sorted(out, key=lambda t: t.address)


# ------------------------------------------------------------------- the read

def _die_name(die):
    attr = die.attributes.get("DW_AT_name")
    return attr.value.decode() if attr else None


def _strip(die, cus):
    """Walk past the qualifiers to the type that has a size."""
    while die is not None and die.tag in ("DW_TAG_const_type",
                                          "DW_TAG_volatile_type",
                                          "DW_TAG_typedef"):
        ref = die.attributes.get("DW_AT_type")
        die = cus[ref.value + die.cu.cu_offset] if ref else None
    return die


def _type_of(die, cus):
    ref = die.attributes.get("DW_AT_type")
    return cus[ref.value + die.cu.cu_offset] if ref else None


def _size(die, cus):
    die = _strip(die, cus)
    if die is None:
        return 0
    attr = die.attributes.get("DW_AT_byte_size")
    if attr:
        return attr.value
    if die.tag == "DW_TAG_pointer_type":
        return 4
    if die.tag == "DW_TAG_array_type":
        return _size(_type_of(die, cus), cus) * _count(die)
    raise SystemExit("abi/shapes.py: %s has no size" % die.tag)


def _count(die):
    for child in die.iter_children():
        if child.tag != "DW_TAG_subrange_type":
            continue
        upper = child.attributes.get("DW_AT_upper_bound")
        if upper is not None:
            return upper.value + 1
        length = child.attributes.get("DW_AT_count")
        if length is not None:
            return length.value
    raise SystemExit("abi/shapes.py: array with no bound")


def _points_to(die, cus):
    target = _strip(_type_of(die, cus), cus)
    if target is None:
        return "void"
    if target.tag == "DW_TAG_subroutine_type":
        return "code"
    if target.tag == "DW_TAG_base_type" and _die_name(target) == "char":
        return "char"
    return "data"


def _spell(die, cus):
    """The C text of a type, which is what a cast in a generated source needs."""
    if die is None:
        return "void"
    if die.tag == "DW_TAG_const_type":
        return "const " + _spell(_type_of(die, cus), cus)
    if die.tag == "DW_TAG_volatile_type":
        return "volatile " + _spell(_type_of(die, cus), cus)
    if die.tag == "DW_TAG_pointer_type":
        inner = _spell(_type_of(die, cus), cus)
        return inner + ("*" if inner.endswith("*") else " *")
    if die.tag == "DW_TAG_structure_type":
        return "struct %s" % _die_name(die)
    if die.tag == "DW_TAG_union_type":
        return "union %s" % _die_name(die)
    if die.tag == "DW_TAG_enumeration_type":
        return "enum %s" % _die_name(die)
    if die.tag == "DW_TAG_array_type":
        return "%s[%d]" % (_spell(_type_of(die, cus), cus), _count(die))
    name = _die_name(die)
    if name is None:
        raise SystemExit("abi/shapes.py: unnamed %s" % die.tag)
    return name


def _element(die, cus):
    """(row struct name or None, what one element is, what a pointer points at)."""
    if die.tag == "DW_TAG_structure_type":
        return _die_name(die), STRUCT, None
    if die.tag == "DW_TAG_pointer_type":
        return None, POINTER, _points_to(die, cus)
    return None, SCALAR, None


def _object(die, cus):
    declared = _type_of(die, cus)
    base = _strip(declared, cus)
    count = 1
    if base is not None and base.tag == "DW_TAG_array_type":
        count = _count(base)
        element = _strip(_type_of(base, cus), cus)
    else:
        element = base
    name, kind, points_to = _element(element, cus)
    return Object(_die_name(die), _spell(declared, cus), _size(base, cus),
                  name, count, kind, points_to)


def _field(member, cus):
    at = member.attributes["DW_AT_data_member_location"].value
    name = _die_name(member)
    declared = _type_of(member, cus)
    base = _strip(declared, cus)
    ctype = _spell(declared, cus)
    if base.tag == "DW_TAG_array_type":
        element = _type_of(base, cus)
        unit = _size(element, cus)
        count = _count(base)
        return Field(name, at, unit * count, ARRAY, _spell(element, cus),
                     count, unit)
    if base.tag == "DW_TAG_pointer_type":
        return Field(name, at, 4, POINTER, ctype,
                     points_to=_points_to(base, cus))
    return Field(name, at, _size(base, cus), SCALAR, ctype)


DECLARED = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


def declared_names(include=INCLUDE):
    """Every identifier abi/include/withings/*.h spells in a declaration."""
    out = set()
    root = os.path.join(include, "withings")
    for name in sorted(os.listdir(root)):
        if not name.endswith(".h"):
            continue
        for line in open(os.path.join(root, name)):
            if not line.startswith(("extern", "typedef", "static inline")):
                continue
            out.update(DECLARED.findall(line))
    return out


def compile_headers(out=None, include=INCLUDE):
    """One object with nothing in it but the debug information for the headers."""
    out = out or os.path.join(SIM, "out", "abi-types.o")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    probe = out + ".c"
    with open(probe, "w") as fh:
        fh.write('/* Generated by abi/shapes.py: the translation unit whose only\n'
                 '   product is the debug information for the hand headers. */\n'
                 '#include "withings/all.h"\n')
    subprocess.check_call(
        [GCC] + ARCH + ["-g3", "-fno-eliminate-unused-debug-types", "-w",
                        "-I", include, "-c", probe, "-o", out])
    return out


def load(include=INCLUDE, out=None):
    """Compile the headers and read the shapes back."""
    if not os.path.exists(GCC):
        sys.exit("abi/shapes.py: %s is missing; run abi/refbuild.sh" % GCC)
    path = compile_headers(out, include)
    with open(path, "rb") as fh:
        dwarf = ELFFile(fh).get_dwarf_info()
        cus, roots = {}, []
        for cu in dwarf.iter_CUs():
            for die in cu.iter_DIEs():
                cus[die.offset] = die
                roots.append(die)
        structs, objects, enums = {}, {}, {}
        for die in roots:
            if die.tag == "DW_TAG_structure_type" and _die_name(die):
                structs[_die_name(die)] = Struct(
                    _die_name(die), die.attributes["DW_AT_byte_size"].value,
                    [_field(m, cus) for m in die.iter_children()
                     if m.tag == "DW_TAG_member"])
            elif die.tag == "DW_TAG_enumeration_type" and _die_name(die):
                enums[_die_name(die)] = [_die_name(e) for e in die.iter_children()]
            elif die.tag == "DW_TAG_variable" and _die_name(die):
                objects[_die_name(die)] = _object(die, cus)
    return Types(structs, objects, enums, declared_names(include))


def main():
    types = load()
    print("%d structs, %d declared objects, %d declared names"
          % (len(types.structs), len(types.objects), len(types.declared)))
    for name in sorted(types.objects):
        o = types.objects[name]
        print("  %-36s %-40s %5d bytes%s"
              % (name, o.ctype, o.size,
                 " (%d x %d)" % (o.count, o.stride) if o.count > 1 else ""))


if __name__ == "__main__":
    main()
