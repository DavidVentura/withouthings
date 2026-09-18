#!/usr/bin/env python3
"""The WPP wire format as the firmware writes it, against the crate's decoder.

    python3 abi/protocol.py            # -> abi/out/protocol/{objects,commands}.json
    python3 abi/protocol.py --struct-uses ecg=0x20011668 [--module ECG]

Two questions, one image and one crate each.

Objects. A WPP object on the wire is {u16 type, u16 length, data}. The image
builds one through a cursor: a `void **` whose target is bumped by every write,
and a family of leaf primitives that take it (abi/autonames.py's wppobj class
finds the encoders by the first two of those calls writing a literal type and a
literal length). Each primitive is one field of one width, so the calls a body
makes after its header, in address order, are the object's field layout read off
the image. The parsers are the same with the getters, reached through the TLV
walk the callback is handed to.

The crate's side is wpp/src/objects.rs, whose `write` and `parse` bodies are
straight-line sequences of `w.u16(..)` / `r.u16()?`, so the same list comes out
of a regex. Comparing the two lists field by field is the conformance test:
`w.u16` and the image's 0x9a954 are the same two big-endian bytes, `w.i16` is
too (signedness is invisible on the wire and is not compared), and a length or
an ordering difference is a real disagreement between this repo's decoder and
the firmware.

Structs outside the protocol. The register model the codecs are read with does
not care that its base came from an argument: seeded from a pc-relative load of
a known global instead, the same walk reports every field access a module makes
through that global, with the displacement, the width and the signedness the
instruction carries. --struct-uses is that, and it is what recovered the
sensor-sync rings, the ECG session and the HR contexts.

Commands. A WPP command handler is a row of wpp_cmd_table. What it parses is the
object types the TLV walk is given inside it; what it replies is the object the
send path is handed. Both are literals somewhere in the handler or in a helper
it calls, so a closure over the call graph gives every command its request and
reply types, which is then compared with wpp/src/commands.rs.
"""

import argparse
import collections
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
ROOT = os.path.dirname(SIM)
sys.path.insert(0, HERE)

import autonames as A  # noqa: E402

# The cursor primitives, established by their bodies at 0x9a930..0x9a9be: each
# stores through *cursor and advances it by the width it wrote, so the name is
# the width and the byte order, and nothing else.
WRITERS = {
    0x9A930: "u8",        # strb, +1
    0x9A94A: "u8",        # uxtb then 0x9a930
    0x9A954: "u16",       # two strb, big-endian, +2
    0x9A978: "u16",       # uxth then 0x9a954
    0x9A982: "u32",       # four strb, big-endian, +4
    0x9A9BA: "u32",       # tail call to 0x9a982
    0x98D72: "string",    # u8 length capped at 0x40, then that many bytes
    0x98D98: "array_u8",  # u8 count, then count u8 through 0x9a930
    0x98DB8: "array_u32",  # u8 count, then count u32 through 0x9a982
}

READERS = {
    0x9A93C: "u8",
    0x9A950: "u8",
    0x9A966: "u16",
    0x9A97E: "u16",
    0x9A9A0: "u32",
    0x9A9BE: "u32",
    0x4ECA4: "string",     # u8 length capped at 0x40, then that many bytes
    0x4ECE8: "array_u8",   # u8 count clamped to the caller's maximum
    0x4ED3C: "array_u32",  # u8 count clamped, then that many big-endian u32
}

# The crate's Writer/Reader methods, by the bytes they move. i16 and u16 are one
# wire field: the image cannot say which the firmware meant.
CRATE_WIDTH = {
    "u8": "u8", "i8": "u8",
    "u16": "u16", "i16": "u16",
    "u32": "u32", "i32": "u32",
    "u64": "u64", "i64": "u64",
    "string": "string", "bytes": "string",
    "array_u8": "array_u8", "array_i16": "array_i16", "array_u16": "array_u16",
    "array_i32": "array_u32", "array_u32": "array_u32",
}

OBJ_IMPL = re.compile(
    r"impl WppObjectCodec for (\w+)\s*\{(.*?)\n\}\n", re.S)
CONSTS = re.compile(
    r"const TYPE_ID: u16 = (\d+);.*?const TYPE_NAME: &'static str = \"(\w+)\";"
    r".*?const FIXED_DATA_SIZE: Option<usize> = (?:Some\((\d+)\)|None);", re.S)
WRITE_BODY = re.compile(r"fn write\(&self, _?w: &mut Writer\) \{(\}|.*?\n    \})", re.S)
PARSE_BODY = re.compile(r"fn parse\(.*?\) -> Result<Self, ParseError> \{(.*?)\n    \}", re.S)
W_CALL = re.compile(r"\bw\.(\w+)\(")
R_CALL = re.compile(r"\br\.(\w+)\(")
# The struct field each wire field comes from: `w.u32(self.uid)` and
# `w.array_u32(&self.user_id)` are the crate's own names for the two sides of
# the same position, which is what names the recovered in-memory field.
W_FIELD = re.compile(r"\bw\.(\w+)\(\s*&?(?:self\.(?:r#)?(\w+)|[^)]*)")
R_FIELD = re.compile(r"(?:(\w+):\s*)?\br\.(\w+)\(")


def crate_objects():
    """{type id: {name, wire fields written, wire fields parsed, fixed size}}."""
    text = open(os.path.join(ROOT, "wpp", "src", "objects.rs"), errors="ignore").read()
    out = {}
    for m in OBJ_IMPL.finditer(text):
        name, body = m.group(1), m.group(2)
        c = CONSTS.search(body)
        if not c:
            continue
        rec = {"rust": name, "type_name": c.group(2),
               "fixed_data_size": int(c.group(3)) if c.group(3) else None}
        w = WRITE_BODY.search(body)
        rec["crate_write"] = [CRATE_WIDTH[k] for k in W_CALL.findall(w.group(1))
                              if k in CRATE_WIDTH] if w else None
        rec["crate_write_raw"] = ([k for k in W_CALL.findall(w.group(1))] if w else None)
        p = PARSE_BODY.search(body)
        rec["crate_parse"] = [CRATE_WIDTH[k] for k in R_CALL.findall(p.group(1))
                              if k in CRATE_WIDTH] if p else None
        wnames = [n for k, n in W_FIELD.findall(w.group(1))
                  if k in CRATE_WIDTH] if w else []
        rnames = [n for n, k in R_FIELD.findall(p.group(1))
                  if k in CRATE_WIDTH] if p else []
        rec["crate_field_names"] = wnames if any(wnames) else rnames
        rec["crate_parse_names"] = rnames
        out[int(c.group(1))] = rec
    return out


class Body:
    """The image's instructions grouped by the function the export says owns them."""

    def __init__(self, img, ex):
        self.img, self.ex = img, ex
        self.insns = collections.defaultdict(list)
        for i, (addr, mnem, ops) in enumerate(img.insns):
            fn = ex.owner(addr)
            if fn is not None:
                self.insns[fn].append((i, addr, mnem, ops))

    def calls(self, fn):
        """(address, target) for every call or tail call the body makes."""
        out = []
        for _, addr, mnem, ops in self.insns.get(fn, ()):
            if mnem in ("bl", "bl.w", "b.w", "b"):
                t = A._call_target(ops)
                if t is not None and t != fn:
                    out.append((addr, t))
        return out

    def in_loop(self, fn, addr):
        """Whether a backward branch inside the body jumps to at or before addr.

        A repeated field is written from a loop, and the only thing the image
        offers to tell one write in a loop from a straight-line write is that
        some later branch in the same body targets an earlier address.
        """
        for _, a, mnem, ops in self.insns.get(fn, ()):
            if a <= addr or not mnem.startswith("b"):
                continue
            t = A._call_target(ops)
            if t is not None and t <= addr and self.ex.owner(t) == fn:
                return True
        return False


def image_fields(body, fn, table, skip_header):
    """The wire fields a codec body moves, in address order."""
    fields, seen_header = [], 0
    for addr, target in body.calls(fn):
        kind = table.get(target)
        if kind is None:
            continue
        if seen_header < skip_header:
            seen_header += 1
            continue
        fields.append({"at": "0x%x" % addr, "type": kind,
                       "repeated": body.in_loop(fn, addr)})
    return fields


# ---------------------------------------------------------------- in-memory
# The codecs' own calling convention, read off the bodies. An encoder opens
# `mov r4, r1; mov r5, r0`, writes its type and length through r4 and then loads
# every field from r5, so r0 is the object and r1 the cursor. A parser opens
# `mov r4, r2; mov r5/r6, r0; mov r_, r1` and compares r1 against the type's
# fixed data size before handing r0 to a getter, so r0 is the cursor, r1 the
# wire length and r2 the object. The scalar getters take (out, cursor) and the
# string and array getters take (cursor, out, max), which is the only place the
# two families differ.
ENC_ARGS = {"obj": "r0", "cur": "r1"}
GETTER_CURSOR_IN_R1 = ("u8", "u16", "u32")

LOAD_OPS = {"ldrb": (1, False), "ldrb.w": (1, False), "ldrsb.w": (1, True),
            "ldrh": (2, False), "ldrh.w": (2, False), "ldrsh.w": (2, True),
            "ldr": (4, False), "ldr.w": (4, False)}
MOV_OPS = ("mov", "mov.w", "uxtb", "uxth", "sxtb", "sxth")
_LOAD = re.compile(r"^(r\d+), \[(r\d+)(?:, #(0x[0-9a-f]+|\d+))?\]$")
_ADDI = re.compile(r"^(r\d+), (r\d+), #(0x[0-9a-f]+|\d+)$")
_ADDR = re.compile(r"^(r\d+), (r\d+), (r\d+)$")
_MOVR = re.compile(r"^(r\d+), (r\d+)$")
_MOVI = re.compile(r"^(r\d+), #(0x[0-9a-f]+|\d+)$")
# `ldrh r1, [r6], #2` -- a codec that walks its own struct with a post-indexed
# load reads the field at the base and leaves the base one field further on.
_LOADPI = re.compile(r"^(r\d+), \[(r\d+)\], #(0x[0-9a-f]+|\d+)$")


def _walk_regs(insns, init):
    """(address, registers) at every call the body makes.

    A tiny abstract interpreter over the only instructions these bodies use:
    the codecs are straight-line movs, immediate loads and immediate-offset
    loads off one base, so a register is either a copy of an argument, a
    constant offset from one, a field loaded from one, or unknown.
    """
    regs = dict(init)
    for _, addr, mnem, ops in insns:
        if mnem in ("bl", "bl.w", "b.w", "b"):
            yield addr, dict(regs)
            if mnem in ("bl", "bl.w"):
                for r in ("r0", "r1", "r2", "r3", "r12"):
                    regs.pop(r, None)
            continue
        if mnem in ("push", "pop", "pop.w", "cmp", "cbz", "cbnz", "bx", "it", "nop"):
            continue
        m = _MOVR.match(ops)
        if m and mnem in MOV_OPS:
            d, src = m.groups()
            regs[d] = regs[src] if src in regs else None
            continue
        m = _MOVI.match(ops)
        if m and mnem.startswith("mov"):
            regs[m.group(1)] = ("imm", int(m.group(2), 0))
            continue
        m = _LOAD.match(ops)
        if m and mnem in LOAD_OPS:
            d, base, off = m.groups()
            b = regs.get(base)
            width, signed = LOAD_OPS[mnem]
            regs[d] = (("field", b[1], b[2] + (int(off, 0) if off else 0), width, signed)
                       if b and b[0] == "ptr" else None)
            continue
        m = _LOADPI.match(ops)
        if m and mnem in LOAD_OPS:
            d, base, step = m.groups()
            b = regs.get(base)
            width, signed = LOAD_OPS[mnem]
            regs[d] = ("field", b[1], b[2], width, signed) if b and b[0] == "ptr" else None
            if b and b[0] == "ptr":
                regs[base] = ("ptr", b[1], b[2] + int(step, 0))
            continue
        m = _ADDI.match(ops) or _ADDR.match(ops)
        if m and mnem in ("adds", "add", "add.w"):
            d, base, third = m.groups()
            b, step = regs.get(base), None
            if third.startswith("r"):
                t = regs.get(third)
                step = t[1] if t and t[0] == "imm" else None
            else:
                step = int(third, 0)
            regs[d] = (("ptr", b[1], b[2] + step)
                       if b and b[0] == "ptr" and step is not None else None)
            continue
        m = re.match(r"^(r\d+)[,\s]", ops)
        if m:
            regs[m.group(1)] = None
    return


WIDTH = {"u8": 1, "u16": 2, "u32": 4}


def _slot(kind, value, count, side):
    """(offset, C width, element count, signed) the codec argument names, or None.

    A scalar moves one field of the primitive's width; a string is the 0x41
    bytes wpp_put_string's 0x40 clamp plus its length byte occupy; an array is
    the primitive's element repeated the literal count the call carries, which
    is the writer's r2 and the getter's maximum. Only the encoder can say a
    field is signed, because only it loads it: `ldrsh` against `ldrh` is the
    whole of the evidence, and a parser stores through a pointer either way.
    """
    if kind in WIDTH:
        if side == "encode":
            if not value or value[0] != "field":
                return None
            return value[2], value[3], 1, value[4]
        if not value or value[0] != "ptr":
            return None
        return value[2], WIDTH[kind], 1, False
    if not value or value[0] != "ptr":
        return None
    if kind == "string":
        return value[2], 1, 0x41, False
    if not count or count[0] != "imm":
        return None
    return value[2], {"array_u8": 1, "array_u32": 4}[kind], count[1], False


def codec_layout(body, ops_at, fn, side):
    """The in-memory slots a codec body touches, in wire order.

    Wire order is what the conformance check already matched against the
    crate, so the n-th slot here is the n-th crate field.
    """
    insns = body.insns.get(fn, ())
    table = WRITERS if side == "encode" else READERS
    init = ({"r0": ("ptr", "obj", 0), "r1": ("ptr", "cur", 0)} if side == "encode"
            else {"r0": ("ptr", "cur", 0), "r2": ("ptr", "obj", 0)})
    out, header = [], 0
    for addr, regs in _walk_regs(insns, init):
        kind = table.get(_call_target_at(ops_at, addr))
        if kind is None:
            continue
        if side == "encode":
            if header < 2:            # the type and the length are literals
                header += 1
                continue
            cursor, value, count = regs.get("r0"), regs.get("r1"), regs.get("r2")
        elif kind in GETTER_CURSOR_IN_R1:
            cursor, value, count = regs.get("r1"), regs.get("r0"), None
        else:
            cursor, value, count = regs.get("r0"), regs.get("r1"), regs.get("r2")
        slot = _slot(kind, value, count, side)
        rec = {"at": "0x%x" % addr, "wire": kind,
               "repeated": body.in_loop(fn, addr)}
        if cursor != ("ptr", "cur", 0) or slot is None:
            rec["computed"] = True
        else:
            off, width, n, signed = slot
            rec.update(offset=off, width=width, count=n, signed=signed)
        out.append(rec)
    return out


def _call_target_at(ops_at, addr):
    ops = ops_at.get(addr)
    return A._call_target(ops) if ops else None


SCALAR_NAME = {(1, False): "u8", (1, True): "i8", (2, False): "u16",
               (2, True): "i16", (4, False): "u32", (4, True): "i32"}


def struct_fields(slots, names):
    """(fields, complaints) for one side's slots: C fields with padding filled in."""
    fields, why, end = [], [], 0
    for i, s in enumerate(slots):
        name = names[i] if i < len(names) and names[i] else "field_%d" % i
        if s.get("computed") or "offset" not in s:
            why.append("%s (%s at %s) is computed, not copied from the struct"
                       % (name, s["wire"], s["at"]))
            continue
        off, width, n = s["offset"], s["width"], s["count"]
        if off < end:
            why.append("%s at +0x%x overlaps the field before it" % (name, off))
            continue
        if off > end:
            fields.append(["u8[0x%x]" % (off - end), "pad_%x" % end])
        base = SCALAR_NAME[(width, s["signed"])]
        fields.append([base if n == 1 else "%s[0x%x]" % (base, n), name])
        end = off + width * n
    return fields, why, end


def recover_structs(body, ops_at, rows):
    """Per type id, the in-memory struct both codecs agree on."""
    out = {}
    for tid, r in sorted(rows.items()):
        if tid is None:
            continue
        rec = {"type_id": tid, "rust": r["rust"], "name": "wpp_%s" % r["rust"]}
        names = r.get("crate_field_names") or []
        sides = {}
        for side, addrs in (("encode", r.get("encoders", [])),
                            ("parse", [r["parser"]] if "parser" in r else [])):
            for a in addrs:
                slots = codec_layout(body, ops_at, int(a, 16), side)
                fields, why, size = struct_fields(slots, names)
                sides.setdefault(side, []).append(
                    {"at": a, "slots": slots, "fields": fields, "why": why, "size": size})
        rec["sides"] = sides
        enc = sides.get("encode", [])
        par = sides.get("parse", [])
        # Only the encoder loads, so only the encoder can say a field is
        # signed; the shapes are compared with the signedness dropped and a
        # width or an offset difference is what counts as a disagreement.
        def shape(fields):
            return [[f[0].replace("i", "u", 1), f[1]] for f in fields]
        shapes = {json.dumps(shape(s["fields"])) for s in enc + par}
        rec["agree"] = len(shapes) <= 1
        if enc:
            rec["fields"], rec["size"] = enc[0]["fields"], enc[0]["size"]
        elif par:
            rec["fields"], rec["size"] = par[0]["fields"], par[0]["size"]
        else:
            rec["fields"], rec["size"] = [], 0
        rec["why"] = sorted({w for s in enc + par for w in s["why"]})
        rec["complete"] = bool(rec["fields"] or not names) and not rec["why"] and rec["agree"]
        out[tid] = rec
    return out


# ------------------------------------------------------- the generated header

# The image's own spelling of a field width, as C. The recovered layouts are
# the only place it survives: everything else is a hand header the compiler
# reads directly.
C_SCALARS = {"i8": "signed char", "u8": "unsigned char", "i16": "short",
             "u16": "unsigned short", "i32": "int", "u32": "unsigned int",
             "char": "char", "float": "float", "f64": "double"}


def c_field(kind, name):
    kind = str(kind).strip()
    suffix = ""
    if kind.endswith("]"):
        kind, _, count = kind.partition("[")
        suffix = "[%s]" % count.rstrip("]")
    return "    %s %s%s;" % (C_SCALARS[kind], name, suffix)


def wrap_note(text, width=74, lead="/* ", cont="   "):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width - len(lead):
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    lines.append(cur)
    if len(lines) == 1:
        return ["%s%s */" % (lead, lines[0])]
    return [lead + lines[0]] + [cont + l for l in lines[1:]] + [cont + "*/"]


def object_evidence(tid, r):
    ev = []
    for side, label in (("encode", "encoder"), ("parse", "parser")):
        for sd in r["sides"].get(side, []):
            sites = " ".join(
                "+0x%x@%s" % (s["offset"], s["at"]) if "offset" in s else "?@%s" % s["at"]
                for s in sd["slots"])
            ev.append("%s %s%s" % (label, sd["at"], (" (%s)" % sites) if sites else ""))
    return ("WPP object type %d (%s), %d bytes; %s."
            % (tid, r["rust"], r["size"], "; ".join(ev)))


def codec_names(r):
    """(side, copy index, name) for every codec body recovered for one type."""
    for side in ("encode", "parse"):
        for n, sd in enumerate(r["sides"].get(side, [])):
            name = "wpp_obj_%s_%s" % (r["rust"], side)
            if n:  # abi/autonames.py's own spelling for a second copy
                name += "_copy_%s" % sd["at"]
            yield side, name, sd["at"]


def header_text(structs, sizers, enc_names):
    """abi/include/withings/wpp_objects.h: the wire, as C.

    The 159 object layouts and the codecs over them are read off the image's own
    encoder and parser bodies, so they are generated rather than written: a hand
    edit here would be overwritten by the next run against a new export. What is
    hand-written is next door in wpp.h -- the primitives the codecs are built
    out of, the erased send path and the three callback types -- because none of
    it is recovered from anything.
    """
    sized = {}
    for enc, (size, sites) in sizers.items():
        rust = enc_names[enc][len("wpp_obj_"):-len("_encode")]
        sized[rust] = sites
    out = ["/* HWA10 (ScanWatch 2) application firmware v3411: the WPP wire.",
           " *",
           " * Generated by abi/protocol.py from the image's own codec bodies; do",
           " * not edit. What each of these is, not where it is: abi/symbols.yaml",
           " * holds the addresses. */",
           "#ifndef WITHINGS_WPP_OBJECTS_H",
           "#define WITHINGS_WPP_OBJECTS_H",
           "",
           '#include "withings/wpp.h"',
           ""]
    for tid in sorted(structs):
        r = structs[tid]
        out += wrap_note(object_evidence(tid, r))
        out.append("struct %s {" % r["name"])
        out += [c_field(t, n) for t, n in r["fields"]]
        out += ["};", ""]

    out.append("/* codecs */")
    for tid in sorted(structs):
        r = structs[tid]
        for side, name, at in codec_names(r):
            if side == "encode":
                out.append("extern void %s(const struct %s *obj, void **cursor);"
                           % (name, r["name"]))
            else:
                out.append("extern int %s(void **cursor, unsigned short len,"
                           " struct %s *out);" % (name, r["name"]))
    out.append("")

    out.append("/* reply sizers: the byte count the object's reply carries, a")
    out.append("   `movs r0, #N; bx lr` stub, handed to the send path in r2")
    out.append("   alongside the encoder in r3. */")
    for rust in sorted(sized):
        out.append("extern int wpp_obj_%s_size(const void *obj);" % rust)
    out.append("")

    out += ["/* typed send path: one inline wrapper per object type the image both",
            "   sizes and encodes, over wpp_send_object_alias's erased pair. */"]
    for tid in sorted(structs):
        r = structs[tid]
        if r["rust"] not in sized or not r["sides"].get("encode"):
            continue
        out.append("static inline void wpp_send_%s(unsigned short command,"
                   " const struct %s *obj)" % (r["rust"], r["name"]))
        out.append("{")
        out.append("    wpp_send_object_alias(obj, command, wpp_obj_%s_size,"
                   % r["rust"])
        out.append("                          (wpp_obj_encoder)wpp_obj_%s_encode);"
                   % r["rust"])
        out.append("}")
    out += ["", "#endif"]
    return "\n".join(out) + "\n"


def symbols_yaml(structs, sizers, enc_names):
    """The codec addresses as abi/symbols.yaml entries, a block to paste.

    The map is hand-edited for everything it says about a name, so this prints
    the entries rather than rewriting the file; re-running it against a later
    export is how an address is re-checked.
    """
    lines = []
    for tid in sorted(structs):
        r = structs[tid]
        for side, name, at in codec_names(r):
            lines += ["- address: %s" % at,
                      "  name: %s" % name,
                      "  kind: function",
                      "  class: codec",
                      "  module: wpp_objects",
                      "  evidence: WPP object type %d (%s), %s"
                      % (tid, r["rust"], side)]
    for enc in sorted(sizers, key=lambda a: enc_names[a]):
        size, sites = sizers[enc]
        rust = enc_names[enc][len("wpp_obj_"):-len("_encode")]
        lines += ["- address: 0x%x" % size,
                  "  name: wpp_obj_%s_size" % rust,
                  "  kind: function",
                  "  class: codec",
                  "  module: wpp_objects",
                  "  evidence: >-",
                  "      the byte count %s's reply carries, a `movs r0, #N;" % rust,
                  "      bx lr` stub. It is the r2 the send path is handed",
                  "      alongside wpp_obj_%s_encode in r3 at %s."
                  % (rust, ", ".join("0x%x" % a for a in sites))]
    return "\n".join(lines)


# ---------------------------------------------------------- structs by module
# The same register model, pointed at a struct rather than at a cursor. A codec
# is walked from a known argument; a module's state is reached through a pc-
# relative literal instead, so the seed is "this register holds the global at
# address A" and everything the walk then does with it -- an immediate-offset
# load or store, an add that makes a second base, a scaled index -- is one field
# access with a displacement and a width. Nothing is inferred across a call:
# AAPCS clobbers r0..r3 and r12, so a pointer that leaves in an argument
# register is simply lost, which is why this reports sites rather than types.
STORE_OPS = {"strb": (1, False), "strb.w": (1, False),
             "strh": (2, False), "strh.w": (2, False),
             "str": (4, False), "str.w": (4, False)}
_STORE = re.compile(r"^(r\d+), \[(r\d+)(?:, #(0x[0-9a-f]+|\d+))?\]$")
_LOADD = re.compile(r"^(r\d+), (r\d+), \[(r\d+)(?:, #(0x[0-9a-f]+|\d+))?\]$")
_INDEX = re.compile(r"^(r\d+), \[(r\d+), (r\d+)(?:, lsl #(\d+))?\]$")
_POOL = re.compile(r"@ 0x([0-9a-f]+)")


def _pool_target(img, ops):
    """The address a `ldr rX, [pc, #N]` reads, or None. The listing annotates
    the slot with its file offset, so the image base is added back."""
    if "[pc" not in ops:
        return None
    m = _POOL.search(ops)
    return int(m.group(1), 16) + img.base if m else None


def struct_uses(img, ex, fns, bases):
    """Every field access the given functions make through the given globals.

    `bases` is {address: name}. A register becomes ("ptr", name, offset) when a
    literal pool load brings the address in, and stays one through moves and
    immediate adds; each load or store through it is recorded as
    (name, offset, width, signed, kind, address, function).
    """
    out = []
    by_fn = collections.defaultdict(list)
    for i, (addr, mnem, ops) in enumerate(img.insns):
        fn = ex.owner(addr)
        if fn in fns:
            by_fn[fn].append((i, addr, mnem, ops))
    for fn in sorted(by_fn):
        regs = {}
        for _, addr, mnem, ops in by_fn[fn]:
            if mnem in ("bl", "bl.w"):
                for r in ("r0", "r1", "r2", "r3", "r12"):
                    regs.pop(r, None)
                continue
            if mnem.startswith("ldr") and "[pc" in ops:
                d = ops.split(",")[0].strip()
                slot = _pool_target(img, ops)
                w = img.word(slot) if slot is not None else None
                regs[d] = ("ptr", bases[w], 0) if w in bases else None
                continue
            m = _INDEX.match(ops)
            if m and (mnem in LOAD_OPS or mnem in STORE_OPS):
                d, base, _, shift = m.groups()
                b = regs.get(base)
                if b and b[0] == "ptr":
                    width, signed = (LOAD_OPS if mnem in LOAD_OPS else STORE_OPS)[mnem]
                    out.append((b[1], b[2], width, signed,
                                "index<<%s" % (shift or "0"),
                                "read" if mnem in LOAD_OPS else "write",
                                "0x%x" % addr, "0x%x" % fn))
                if mnem in LOAD_OPS:
                    regs[d] = None
                continue
            m = _LOADD.match(ops)
            if m and mnem in ("ldrd", "strd"):
                d0, d1, base, off = m.groups()
                b = regs.get(base)
                if b and b[0] == "ptr":
                    o = b[2] + (int(off, 0) if off else 0)
                    for k in (0, 4):
                        out.append((b[1], o + k, 4, False, "",
                                    "read" if mnem == "ldrd" else "write",
                                    "0x%x" % addr, "0x%x" % fn))
                if mnem == "ldrd":
                    regs[d0] = regs[d1] = None
                continue
            m = _STORE.match(ops)
            if m and mnem in STORE_OPS:
                src, base, off = m.groups()
                b = regs.get(base)
                if b and b[0] == "ptr":
                    width, signed = STORE_OPS[mnem]
                    out.append((b[1], b[2] + (int(off, 0) if off else 0), width,
                                signed, "", "write", "0x%x" % addr, "0x%x" % fn))
                continue
            m = _LOAD.match(ops)
            if m and mnem in LOAD_OPS:
                d, base, off = m.groups()
                b = regs.get(base)
                width, signed = LOAD_OPS[mnem]
                if b and b[0] == "ptr":
                    out.append((b[1], b[2] + (int(off, 0) if off else 0), width,
                                signed, "", "read", "0x%x" % addr, "0x%x" % fn))
                regs[d] = None
                continue
            m = _MOVR.match(ops)
            if m and mnem in MOV_OPS:
                d, src = m.groups()
                regs[d] = regs.get(src)
                continue
            m = _ADDI.match(ops)
            if m and mnem in ("adds", "add", "add.w"):
                d, base, off = m.groups()
                b = regs.get(base)
                regs[d] = ("ptr", b[1], b[2] + int(off, 0)) if b and b[0] == "ptr" else None
                continue
            m = re.match(r"^(r\d+)[,\s]", ops)
            if m:
                regs[m.group(1)] = None
    return out


def struct_uses_report(uses):
    """The accesses grouped by global and offset, the way a field table reads."""
    by = collections.defaultdict(list)
    for name, off, width, signed, kind, rw, at, fn in uses:
        by[(name, off)].append((width, signed, kind, rw, at, fn))
    lines = []
    for (name, off) in sorted(by, key=lambda k: (k[0], k[1])):
        rows = by[(name, off)]
        widths = sorted({w for w, _, _, _, _, _ in rows})
        signed = any(s for _, s, _, _, _, _ in rows)
        rw = sorted({r for _, _, _, r, _, _ in rows})
        lines.append("%-32s +0x%-5x %s%s %-11s %s"
                     % (name, off, "i" if signed else "u",
                        "/".join(str(w * 8) for w in widths), ",".join(rw),
                        " ".join(at for _, _, _, _, at, _ in rows)))
    return "\n".join(lines)


def compare(image, crate):
    """The first index at which the two field lists disagree, or None."""
    if crate is None:
        return "crate side missing"
    img = [f["type"] for f in image]
    if img == crate:
        return None
    for i, (a, b) in enumerate(zip(img, crate)):
        if a != b:
            return "field %d: image %s, crate %s" % (i, a, b)
    return ("image has %d fields, crate has %d" % (len(img), len(crate)))



# The send path, established from the bodies at 0x6bd0c..0x9d3d8: 0x6bd0c takes
# (command word, frame) and hands the frame to the link layer, 0x9d322 appends
# one object to a frame as (frame, object, encoder, sizer, terminate), and the
# wrappers in between build a frame on the stack for one object and send it. The
# reply's type is therefore the encoder pointer the wrapper is given, and the
# command is the literal in the wrapper's command register.
SENDERS = {
    0x9D38A: {"cmd": 1, "enc": 3},   # (object, command, sizer, encoder)
    0x9D3D8: {"cmd": 1, "enc": 3},   # b.w 0x9d38a
    0x9D3B4: {"cmd": 0, "enc": None},  # command with an empty object list
    0x6BD94: {"cmd": 0, "enc": None},  # ORs 0x4000 into the word, then 0x6bd0c
    0x6BD54: {"cmd": 0, "enc": None},  # ORs 0x8000 into the word, then 0x6bd0c
    0x6BD0C: {"cmd": 0, "enc": None},
    0x6C7C4: {"cmd": None, "enc": None, "fixed_cmd": 256},  # the error reply
}
# Appending one object to a frame the caller already has: same (sizer, encoder)
# pair in r2/r3, no command of its own, so the reply type is established here
# and the command by whichever send call the same body makes.
APPENDERS = {0x6BB98: 3, 0x9D322: 3, 0x9D330: 3}
WPP_OBJ_PARSE = 0x504B4


def pool_value(img, ops):
    """The word a `ldr rX, [pc, #N]` loads, or None."""
    if "[pc" not in ops:
        return None
    m = re.search(r"@ 0x([0-9a-f]+)", ops)
    return img.word(int(m.group(1), 16) + img.base) if m else None


def _reg_source(img, i, reg, back=24):
    """(literal, pool word) the register was last set to before instruction i."""
    for j in range(i - 1, max(-1, i - back), -1):
        _, mnem, ops = img.insns[j]
        if not re.match(r"^%s[,\s]" % reg, ops):
            if mnem in ("bl", "bl.w", "blx", "b", "b.w", "pop", "bx"):
                return None, None
            continue
        m = re.match(r"^%s,\s*#(0x[0-9a-f]+|\d+)$" % reg, ops)
        if m and mnem.startswith("mov"):
            return int(m.group(1), 0), None
        if mnem.startswith("ldr"):
            return None, pool_value(img, ops)
        return None, None
    return None, None


def command_facts(img, ex, body, encoders):
    """{function: {"sends": {(cmd, reply type)}, "parses": {type}}} per site."""
    facts = collections.defaultdict(
        lambda: {"sends": set(), "parses": set(), "appends": set()})
    for i, (addr, mnem, ops) in enumerate(img.insns):
        if mnem not in ("bl", "bl.w", "b.w", "b"):
            continue
        target = A._call_target(ops)
        fn = ex.owner(addr)
        if fn is None:
            continue
        if target == WPP_OBJ_PARSE:
            tid = A._imm_before(img, i, "r3")
            if tid is not None:
                facts[fn]["parses"].add(tid)
            continue
        if target in APPENDERS:
            _, word = _reg_source(img, i, "r%d" % APPENDERS[target])
            if word and encoders.get(word & ~1) is not None:
                facts[fn]["appends"].add(encoders[word & ~1])
            continue
        spec = SENDERS.get(target)
        if spec is None:
            continue
        cmd = spec.get("fixed_cmd")
        if cmd is None and spec["cmd"] is not None:
            cmd, _ = _reg_source(img, i, "r%d" % spec["cmd"])
        if cmd is None:
            continue
        reply = None
        if spec["enc"] is not None:
            _, word = _reg_source(img, i, "r%d" % spec["enc"])
            if word:
                reply = encoders.get(word & ~1)
        facts[fn]["sends"].add((cmd & 0x3FFF, reply))
    return facts


# The sizer and the encoder travel together: every send and append site loads
# them into r2 and r3 of the same call, so the sizer of a type is whichever stub
# shares a call with that type's encoder. The stubs are one `movs r0,#N; bx lr`
# each and say nothing on their own.
SIZER_SITES = {0x9D38A: (2, 3), 0x9D3D8: (2, 3),
               0x6BB98: (2, 3), 0x9D322: (2, 3), 0x9D330: (2, 3)}


def codec_sizers(img, encoders):
    """{encoder address: (sizer address, [call sites])}, refusing a split."""
    pairs = collections.defaultdict(set)
    for i, (addr, mnem, ops) in enumerate(img.insns):
        if mnem not in ("bl", "bl.w", "b.w", "b"):
            continue
        spec = SIZER_SITES.get(A._call_target(ops))
        if spec is None:
            continue
        _, size = _reg_source(img, i, "r%d" % spec[0])
        _, enc = _reg_source(img, i, "r%d" % spec[1])
        if size and enc and (enc & ~1) in encoders:
            pairs[enc & ~1].add((size & ~1, addr))
    out = {}
    for enc, seen in pairs.items():
        sizes = {s for s, _ in seen}
        if len(sizes) != 1:
            raise SystemExit("protocol: encoder 0x%x is sent with %d sizers: %s"
                             % (enc, len(sizes), ["0x%x" % s for s in sorted(sizes)]))
        out[enc] = (sizes.pop(), sorted(a for _, a in seen))
    return out


def owning_handler(ex, handlers):
    """{function: handler} for every function only one command handler reaches.

    A helper several handlers share says nothing about any one of them, and a
    helper only one handler reaches is that handler's own code, so its literals
    are that command's. The walk is the forward call graph from each handler,
    and a function two walks reach is dropped.
    """
    owner, shared = {}, set()
    for h in handlers:
        seen, stack = set(), [h]
        while stack:
            fn = stack.pop()
            if fn in seen:
                continue
            seen.add(fn)
            stack.extend(ex.callees.get(fn, ()))
        for fn in seen:
            if fn in owner and owner[fn] != h:
                shared.add(fn)
            owner[fn] = h
    return {fn: h for fn, h in owner.items() if fn not in shared}


CMD_CONST = re.compile(r"pub const (\w+): Command = Command\((\d+)\);")


def crate_commands():
    text = open(os.path.join(ROOT, "wpp", "src", "commands.rs"), errors="ignore").read()
    return {int(m.group(2)): m.group(1) for m in CMD_CONST.finditer(text)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", default=os.path.join(HERE, "out", "ghidra"))
    ap.add_argument("--out", default=os.path.join(HERE, "out", "protocol"))
    ap.add_argument("--emit-header", nargs="?", const=os.path.join(
                        HERE, "include", "withings", "wpp_objects.h"),
                    help="write the layouts, the codecs and the typed send path"
                         " as the generated header, committed as generated")
    ap.add_argument("--emit-symbols",
                    help="write the codec addresses as an abi/symbols.yaml block")
    ap.add_argument("--struct-uses", action="append", default=[], metavar="NAME=ADDR",
                    help="report every field access made through this global")
    ap.add_argument("--module", action="append", default=[],
                    help="restrict --struct-uses to the functions of this module")
    args = ap.parse_args()

    img = A.Image(os.path.join(SIM, "appl.bin"), os.path.join(SIM, "out", "appl.dis"))
    ex = A.Export(args.export, A.prior_addresses(args.export))
    if not ex.ok:
        sys.exit("protocol: no export under %s" % args.export)
    if args.struct_uses:
        bases = {}
        for spec in args.struct_uses:
            name, _, addr = spec.partition("=")
            bases[int(addr, 0)] = name
        mods = json.load(open(os.path.join(args.export, "modules.json")))["functions"]
        fns = ({int(a, 16) for a, v in mods.items() if v["module"] in args.module}
               if args.module else set(ex.fns))
        print(struct_uses_report(struct_uses(img, ex, fns, bases)))
        return

    body = Body(img, ex)
    names, _ = A.wpp_object_names(img, ex)
    crate = crate_objects()

    rows = {}
    for e in names:
        rust, side = e["name"][len("wpp_obj_"):].rsplit("_", 1)
        tid = next((t for t, c in crate.items() if c["rust"] == rust), None)
        row = rows.setdefault(tid, dict(crate[tid], type_id=tid))
        if side == "encode":
            # More than one body can encode the same type: Null is written by
            # two, and a second encoder is a finding rather than a collision.
            row.setdefault("encoders", []).append("0x%x" % e["address"])
            row["image_write"] = image_fields(body, e["address"], WRITERS, 2)
        else:
            row["parser"] = "0x%x" % e["address"]
            row["image_parse"] = image_fields(body, e["address"], READERS, 0)

    out = []
    for tid in sorted(rows):
        r = rows[tid]
        r["write_match"] = compare(r["image_write"], r["crate_write"]) if "image_write" in r else None
        r["parse_match"] = compare(r["image_parse"], r["crate_parse"]) if "image_parse" in r else None
        out.append(r)

    ops_at = {a: o for a, _, o in img.insns}
    structs = recover_structs(body, ops_at, rows)

    os.makedirs(args.out, exist_ok=True)
    spath = os.path.join(args.out, "structs.json")
    with open(spath, "w") as fh:
        json.dump({"_generated": "by abi/protocol.py, do not edit",
                   "structs": [structs[t] for t in sorted(structs)]}, fh, indent=1)

    whole = [r for r in structs.values() if r["complete"]]
    split = [r for r in structs.values() if not r["agree"]]
    print("%s: %d object types, %d with a complete in-memory struct, "
          "%d where the encoder and the parser disagree"
          % (spath, len(structs), len(whole), len(split)))
    for r in split:
        print("  %d %s: encoder %s, parser %s"
              % (r["type_id"], r["rust"],
                 [s["at"] for s in r["sides"].get("encode", [])],
                 [s["at"] for s in r["sides"].get("parse", [])]))

    path = os.path.join(args.out, "objects.json")
    with open(path, "w") as fh:
        json.dump({"_generated": "by abi/protocol.py, do not edit", "objects": out},
                  fh, indent=1)
    enc = [r for r in out if "image_write" in r]
    par = [r for r in out if "image_parse" in r]

    # ---- commands
    cmds = crate_commands()
    encoders = {}
    for tid, r in rows.items():
        for a in r.get("encoders", ()):
            encoders[int(a, 16)] = tid
    enc_names = {e["address"]: e["name"] for e in names
                 if e["name"].endswith("_encode")}
    sizers = codec_sizers(img, enc_names)
    print("%d of %d encoders are sent with a sizer the call site pairs them with"
          % (len(sizers), len(enc_names)))
    # The wire is generated in one piece: the layouts, the codecs over them
    # and the sizers the send path pairs with the encoders are all read off the
    # same bodies, so they are written by the same run.
    if args.emit_header:
        with open(args.emit_header, "w") as fh:
            fh.write(header_text(structs, sizers, enc_names))
        print(args.emit_header)
    if args.emit_symbols:
        with open(args.emit_symbols, "w") as fh:
            fh.write(symbols_yaml(structs, sizers, enc_names) + "\n")
        print(args.emit_symbols)

    table = A.walk_table(img, A.WPP_ANCHOR, lambda r: r["key"] < A.WPP_ID_MAX)
    handlers = {r["handler"]: r for r in table}
    facts = command_facts(img, ex, body, encoders)
    owner = owning_handler(ex, set(handlers))

    per = collections.defaultdict(
        lambda: {"parses": set(), "sends": set(), "appends": set()})
    for fn, f in facts.items():
        h = fn if fn in handlers else owner.get(fn)
        if h is None:
            continue
        for k in ("parses", "sends", "appends"):
            per[h][k].update(f[k])

    by_type = {t: r["rust"] for t, r in rows.items()}
    crate_types = {t: c["rust"] for t, c in crate.items()}
    cout = []
    for r in table:
        h = r["handler"]
        f = per.get(h, {"parses": set(), "sends": set(), "appends": set()})
        cout.append({
            "command": r["key"],
            "label": r["text"].decode() if isinstance(r["text"], bytes) else r["text"],
            "handler": "0x%x" % h,
            "crate_name": cmds.get(r["key"]),
            "request_types": sorted(f["parses"]),
            "request_rust": [crate_types.get(t) for t in sorted(f["parses"])],
            "reply_commands": sorted({c for c, _ in f["sends"]}),
            "reply_types": sorted({t for _, t in f["sends"] if t} | f["appends"]),
            "reply_rust": sorted({crate_types.get(t) for t in
                                  {t for _, t in f["sends"] if t} | f["appends"]}),
        })
    cpath = os.path.join(args.out, "commands.json")
    with open(cpath, "w") as fh:
        json.dump({"_generated": "by abi/protocol.py, do not edit",
                   "commands": cout,
                   "crate_only": sorted(set(cmds) - {r["key"] for r in table}),
                   "image_only": sorted({r["key"] for r in table} - set(cmds))},
                  fh, indent=1)
    known = [c for c in cout if c["request_types"] or c["reply_commands"]]
    print("%s: %d table rows, %d with a request or a reply established, "
          "%d rows the crate does not name, %d commands the crate has and the "
          "image does not"
          % (cpath, len(cout), len(known),
             sum(1 for c in cout if not c["crate_name"]),
             len(set(cmds) - {r["key"] for r in table})))

    bad = [r for r in out if r["write_match"] or r["parse_match"]]
    print("%s: %d object types in the image (%d encoders, %d parsers), "
          "%d crate types, %d with a mismatch"
          % (path, len(out), len(enc), len(par), len(crate), len(bad)))
    for r in bad:
        print("  %d %s: write %s; parse %s"
              % (r["type_id"], r["rust"], r["write_match"], r["parse_match"]))


if __name__ == "__main__":
    main()
