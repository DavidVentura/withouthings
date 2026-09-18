#!/usr/bin/env python3
"""Emit the app's data sections as source the toolchain assembles and relocates.

    python3 abi/blobify.py --data-source ../out/data

The code side of the objectified app is already source in the sense that
matters: every branch is a relocation and any single function can be swapped for
a compiled one. The data side is not -- it is bytes blobify copies out of the
image with a relocation list beside them -- so a table cannot be read, reviewed
or edited as anything but a hex dump. This writes each data section as a file
the toolchain turns back into the same bytes, and abi/identity.sh is what says
they are the same bytes.

Why assembly and not C for the byte sections. An image data object is not a C
array: the partition's objects carry labels at interior offsets -- a pointer
into row N of a table, the shared tail of two strings, the entry point of the
copy-in image at 0xefe58 -- and C has no way to name an offset inside an array.
GCC's `alias` attribute binds a whole symbol to a whole symbol, never to an
offset, and a linker-script assignment would lose the section, because a symbol
defined outside a section neither keeps it alive under --gc-sections nor moves
with it. The assembler has exactly the construct that is missing: a label
between two directives. So a byte section is `.byte` runs with `.word <symbol>`
where a relocation is and a label wherever the object names an offset, which is
the object file blobify wrote expressed in the tool's own language.

A typed table is worth the difference: where hwa10.yaml declares the entry
struct, the rows become a C initialiser whose pointer fields name the target
symbols, so the compiler emits the relocations and the layout is the C struct's.
Those go to `<name>.c` and are compiled one file at a time. The identity link
staying byte-identical with them in is the proof that the C layout is the
image's layout.

Determinism: sections are written in address order, one file per section named
after it, and within a file the bytes go out in offset order sixteen to a line.
Nothing iterates a set. `all.s` is the list of `.include`s in the same order, so
one assembler run gives one object with every byte section in it.
"""

import os

APP_BASE = 0x27000

# The relocation a `.reloc` directive has to name, by the number blobify uses.
RELOC_NAMES = {10: "R_ARM_THM_CALL", 30: "R_ARM_THM_JUMP24",
               51: "R_ARM_THM_JUMP19"}

BYTES_PER_LINE = 16


def alignment(addr):
    """The strongest alignment the section's own address already satisfies.

    A section may not be given more alignment than it had: the placement pins
    each one at an address the image chose, and an alignment that address does
    not satisfy would push the location counter forward into its neighbour.
    """
    for a in (4, 2):
        if addr % a == 0:
            return a
    return 1


class Emitted(object):
    """What one section became, for the report and for the build."""

    def __init__(self, section, path, kind):
        self.section, self.path, self.kind = section, path, kind
        self.bytes = section.end - section.start


def render_bytes(section, body, names, words, branches):
    """One section as assembler source.

    `names` is (offset, symbol, exported, is_func) in the order the symbols are to be
    declared; `words` maps an offset to the symbol the word there names, and
    `branches` an offset to the (symbol, relocation) of a call the boundary owns
    that sits in a component the partition did not call code. A relocated word
    is blank in `body`, which is asserted rather than assumed: the addend lives
    in the bytes of a REL object, so a word that still held its old value would
    add that value to the symbol. A branch carries its addend in the
    instruction, so its bytes stay and only the relocation is declared.
    """
    covered = set()
    for off in words:
        covered.update(range(off + 1, off + 4))
    at, computed = {}, []
    for off, name, _, is_func in names:
        # Two label positions have no point in the byte stream to sit at: one
        # inside a relocated word, and one whose value is not its offset because
        # Thumb-ness is bit 0 of a function symbol and is OR-ed in rather than
        # added. An assignment states the value the object holds either way.
        if off in covered or is_func:
            computed.append((name, off | 1 if is_func else off))
            continue
        at.setdefault(off, []).append((name, is_func))
    for off, sym in words.items():
        if any(body[off:off + 4]):
            raise SystemExit("%s: the relocation at +0x%x covers bytes that are"
                             " not blank" % (section.sym, off))
        if off + 4 > len(body):
            raise SystemExit("%s: the relocation at +0x%x leaves the section"
                             % (section.sym, off))

    for off, (sym, kind) in sorted(branches.items()):
        if off + 4 > len(body):
            raise SystemExit("%s: the branch relocation at +0x%x leaves the"
                             " section" % (section.sym, off))

    out = ['\t.section %s,"a",%%progbits' % section.name,
           "\t.balign %d" % alignment(section.start)]
    for off, name, exported, is_func in names:
        if exported:
            out.append("\t.global %s" % name)
        out.append("\t.type %s, %%%s" % (name, "function" if is_func else "object"))

    off, run = 0, []

    def flush():
        while run:
            out.append("\t.byte " + ",".join("0x%02x" % b for b in run[:BYTES_PER_LINE]))
            del run[:BYTES_PER_LINE]

    while off < len(body):
        if off in at:
            flush()
            out.extend("%s:" % name for name, _ in at[off])
        if off in words:
            flush()
            out.append("\t.word %s" % words[off])
            off += 4
            continue
        run.append(body[off])
        off += 1
        if off in at or off in words:
            flush()
    flush()
    # A label on the byte after the last one: the partition gives a run's end an
    # interior name when the object above it ends exactly there.
    for name, _ in at.get(len(body), ()):
        out.append("%s:" % name)
    for name, value in computed:
        out.append("\t.set %s, %s + %d" % (name, section.sym, value))
    for off, (sym, kind) in sorted(branches.items()):
        out.append("\t.reloc %s + %d, %s, %s" % (section.sym, off, kind, sym))
    out.append("\t.size %s, . - %s" % (section.sym, section.sym))
    out.append("")
    return "\n".join(out)


def write(directory, emitted, sources):
    """Write the per-section files, `all.s` and the list the build reads.

    `emitted` is (section, text, kind) in address order. The byte sections go
    into `all.s` as includes so one assembler run produces one object; a typed
    table is its own C file and is compiled on its own, and its path goes into
    `sources.txt` for the build script to read.
    """
    os.makedirs(directory, exist_ok=True)
    stale = set(f for f in os.listdir(directory) if f.endswith((".s", ".c")))
    out, files = [], []
    for section, text, kind in emitted:
        name = "%s.%s" % (section.sym, "c" if kind == "typed" else "s")
        with open(os.path.join(directory, name), "w") as fh:
            fh.write(text)
        stale.discard(name)
        files.append(Emitted(section, name, kind))
        out.append(name)
    with open(os.path.join(directory, "all.s"), "w") as fh:
        fh.write("/* Generated by abi/datagen.py, do not edit: every data"
                 " section of the app as bytes, labels and relocations. */\n")
        for name in out:
            if name.endswith(".s"):
                fh.write('\t.include "%s"\n' % name)
    stale.discard("all.s")
    with open(os.path.join(sources), "w") as fh:
        for name in out:
            if name.endswith(".c"):
                fh.write(os.path.join(directory, name) + "\n")
    # A section that stops being delegated leaves its file behind, and a stale
    # file assembled into the object would define the same section twice.
    for name in sorted(stale):
        os.remove(os.path.join(directory, name))
    return files

WIDTHS = {"u8": 1, "i8": 1, "u16": 2, "i16": 2, "u32": 4, "i32": 4}
C_SCALARS = {"u8": "unsigned char", "i8": "signed char", "u16": "unsigned short",
             "i16": "short", "u32": "unsigned int", "i32": "int"}


class Field(object):
    def __init__(self, offset, width, name, ctype, kind, count):
        self.offset, self.width, self.name = offset, width, name
        self.ctype, self.kind, self.count = ctype, kind, count


def c_type(kind, typedefs):
    """The text gen.py's header gives a manifest field type."""
    kind = str(kind)
    if kind in typedefs:
        return kind, 4
    if kind.endswith("*"):
        head = kind[:-1].strip()
        for short, long in sorted(C_SCALARS.items()):
            if head.endswith(short):
                head = head[:-len(short)] + long
                break
        return head + " *", 4
    return C_SCALARS[kind], WIDTHS[kind]


def entry_fields(fields, typedefs):
    """The struct's fields with their offsets, or None where C would not agree.

    The manifest packs a struct: each field follows the one before it with no
    padding, which is what the image's strides say. A C compiler aligns each
    field to its own width, and where the two disagree the initialiser would
    write the rows somewhere else, so the table stays bytes rather than being
    declared packed behind gen.py's back.
    """
    out, at, natural = [], 0, 0
    for kind, name in fields:
        kind, name = str(kind), str(name)
        if "[" in kind:
            base, _, count = kind.partition("[")
            count = int(count.rstrip("]"))
            ctype, width = c_type(base, typedefs)
            shape, unit = "array", width
            width *= count
        else:
            ctype, width = c_type(kind, typedefs)
            shape, unit, count = ("pointer" if width == 4 and
                                  (kind.endswith("*") or kind in typedefs)
                                  else "scalar"), width, 1
        natural = (natural + unit - 1) // unit * unit
        if natural != at:
            return None
        out.append(Field(at, width, name, ctype, shape, count))
        at += width
        natural += width
    return out


def render_table(section, body, table, fields, words, names):
    """One declared table as a C initialiser, or (None, why) if it cannot be.

    The pointer fields name the symbols rather than holding addresses, so the
    compiler emits the relocations the blob object held and the linker resolves
    them; the integers are literals read straight out of the image. The section
    is named by hand rather than left to -fdata-sections, because the placement
    fragment puts every section at its own address by name and gen.py declares
    these tables without `const`.
    """
    start, stride, count, name, entry = table
    if section.start != start or section.end != start + stride * count:
        return None, ("the section is 0x%x..0x%x, the declaration 0x%x..0x%x"
                      % (section.start, section.end, start, start + stride * count))
    if fields is None:
        return None, "the C layout of %s is not the packed one" % entry
    pointer_at = dict((f.offset, f) for f in fields if f.kind == "pointer")
    for row in range(count):
        for off in words:
            if off // stride == row and (off % stride) not in pointer_at:
                return None, ("a relocation at +0x%x is not on a pointer field"
                              % off)

    def literal(off, width):
        return "0x%0*xu" % (width * 2,
                            int.from_bytes(body[off:off + width], "little"))

    rows = []
    for row in range(count):
        base = row * stride
        parts = []
        for f in fields:
            at = base + f.offset
            if f.kind == "array":
                unit = f.width // f.count
                parts.append(".%s = { %s }"
                             % (f.name, ", ".join(literal(at + i * unit, unit)
                                                  for i in range(f.count))))
            elif f.kind == "pointer":
                if at in words:
                    parts.append(".%s = (%s)%s" % (f.name, f.ctype, words[at]))
                else:
                    parts.append(".%s = (%s)%s" % (f.name, f.ctype,
                                                   literal(at, 4)))
            else:
                parts.append(".%s = %s" % (f.name, literal(at, f.width)))
        rows.append("    { %s },   /* 0x%08x */" % (", ".join(parts),
                                                    start + base))

    used = sorted(set(words.values()))
    # A name the object gives a point inside the table: C cannot put a label at
    # an offset in an array, but the assembler the compiler is writing for can,
    # and file-scope asm lands in the same translation unit as the definition,
    # so the assignment resolves against it.
    interior = []
    for off, sym, exported, is_func in names:
        if off == 0 and not is_func:
            continue
        interior.append((sym, off | 1 if is_func else off, exported))
    out = ["/* Generated by abi/datagen.py, do not edit: %s at 0x%x as the array\n"
           "   abi/hwa10.yaml declares it, one initialiser per row. */"
           % (name, start),
           '#include "hwa10.h"', ""]
    # Every target is declared as bytes rather than by its real prototype: the
    # header already declares some of them and a second declaration of a
    # different shape would not compile, while an address is an address whatever
    # the cast says. Thumb-ness is bit 0 of the defining symbol's value and the
    # relocation carries it either way.
    out.extend("extern const char %s[];" % sym for sym in used
               if not declared_in_header(sym))
    out.append("")
    out.append("struct %s %s[%d]" % (entry, name, count))
    out.append("        __attribute__((section(\"%s\"), aligned(%d))) = {"
               % (section.name, alignment(start)))
    out.extend(rows)
    out.append("};")
    for sym, value, exported in interior:
        if sym == name:
            continue
        out.append('__asm__("%s.set %s, %s + %d");'
                   % (".global %s\\n" % sym if exported else "", sym, name, value))
    out.append("")
    return "\n".join(out), None


HEADER_NAMES = set()


def declared_in_header(name):
    return name in HEADER_NAMES


def read_header(path):
    """The identifiers gen.py's header already declares, so they are not redeclared."""
    import re
    HEADER_NAMES.clear()
    for line in open(path):
        if not line.startswith("extern") and not line.startswith("typedef"):
            continue
        HEADER_NAMES.update(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", line))
    return HEADER_NAMES

