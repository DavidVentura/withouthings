#!/usr/bin/env python3
"""The WPP wire format as the firmware writes it, against the crate's decoder.

    python3 abi/protocol.py            # -> abi/out/protocol/{objects,commands}.json

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
    args = ap.parse_args()

    img = A.Image(os.path.join(SIM, "appl.bin"), os.path.join(SIM, "out", "appl.dis"))
    ex = A.Export(args.export, A.prior_addresses(args.export))
    if not ex.ok:
        sys.exit("protocol: no export under %s" % args.export)
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

    os.makedirs(args.out, exist_ok=True)
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
