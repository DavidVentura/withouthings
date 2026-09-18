#!/usr/bin/env python3
"""Generate hwa10.h and hwa10.ld from hwa10.yaml.

    python3 abi/gen.py [--out DIR]

Matched library symbols (abi/matches.yaml) and mechanically derived names
(abi/autonames.yaml) are folded in, both refusing to contradict the manifest.

The header declares every manifest function as an extern prototype and every
known global as an extern object; the linker script PROVIDEs each name at its
address (Thumb bit set for functions, plain for data). Placement is
abi/relink.ld's job, whose MEMORY mirrors the manifest's regions.
"""

import argparse
import os
import re
import sys

import yaml

# Manifest type -> C type. A type outside this map is an error, not a guess.
SCALARS = {
    "void": "void",
    "bool": "int",
    "i8": "signed char",
    "u8": "unsigned char",
    "i16": "short",
    "u16": "unsigned short",
    "i32": "int",
    "u32": "unsigned int",
    "i64": "long long",
    "u64": "unsigned long long",
    "char": "char",
    "float": "float",
}

ARRAY = re.compile(r"^(?P<base>[A-Za-z_][A-Za-z0-9_ ]*?)\[(?P<n>0x[0-9a-fA-F]+|\d+)\]$")


class ManifestError(Exception):
    pass


def c_type(t, structs, typedefs=()):
    t = t.strip()
    m = ARRAY.match(t)
    if m:
        base, inner = c_type(m.group("base"), structs, typedefs)
        if inner:
            raise ManifestError("nested array type %r" % t)
        return base, "[%s]" % m.group("n")
    stars = ""
    while t.endswith("*"):
        stars = " *" + stars.lstrip()
        t = t[:-1].strip()
    const = ""
    if t.startswith("const "):
        const = "const "
        t = t[len("const "):].strip()
    if t.startswith("struct "):
        name = t[len("struct "):].strip()
        if name not in structs:
            raise ManifestError("unknown struct %r" % name)
        base = "struct %s" % name
    elif t in SCALARS:
        base = SCALARS[t]
    elif t in typedefs:
        base = t
    else:
        raise ManifestError("unknown type %r" % t)
    return const + base + stars, ""


def decl(t, name, structs, typedefs=()):
    base, suffix = c_type(t, structs, typedefs)
    sep = "" if base.endswith("*") else " "
    return "%s%s%s%s" % (base, sep, name, suffix)


def check_unique(seen, address, name, kind):
    if address in seen:
        raise ManifestError("duplicate address 0x%x: %s and %s" % (address, seen[address], name))
    seen[address] = "%s %s" % (kind, name)


# A C identifier; a match like "prvInitialiseNewTask.isra.0" is a GCC clone of a
# static, and neither its name nor its ABI is the source function's.
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_matches(path, threshold, seen_fn, seen_obj, manifest):
    """Matched symbols good enough to enter the manifest.

    A hand-written entry always wins on content but never silently: a match that
    contradicts one is an error, because one of the two is wrong and picking
    either would hide that.
    """
    if not os.path.exists(path):
        return []
    with open(path) as f:
        mm = yaml.safe_load(f)
    by_name = {fn["name"]: fn["address"] & ~1 for fn in manifest["functions"]}
    # A hand entry may correct a match, and says which address it corrects, so
    # the correction only holds against the measurement it was made against: if
    # abi/match.py is re-run and lands somewhere else, the refusal below fires
    # again instead of the stale correction winning silently.
    corrected = {fn["name"]: fn["corrects"] & ~1
                 for fn in manifest["functions"] if "corrects" in fn}
    out = []
    for fn in mm.get("functions", []):
        if fn["score"] < threshold or not IDENT.match(fn["name"]):
            continue
        addr = fn["address"] & ~1
        if fn["name"] in by_name:
            if corrected.get(fn["name"]) == addr:
                continue
            if by_name[fn["name"]] != addr:
                raise ManifestError(
                    "matches.yaml puts %s at 0x%x, hwa10.yaml at 0x%x"
                    % (fn["name"], addr, by_name[fn["name"]]))
            continue
        if addr in seen_fn:
            raise ManifestError(
                "matches.yaml names 0x%x %s, hwa10.yaml already has %s there"
                % (addr, fn["name"], seen_fn[addr]))
        if addr in seen_obj:
            raise ManifestError(
                "matches.yaml names 0x%x %s, hwa10.yaml has data %s there"
                % (addr, fn["name"], seen_obj[addr]))
        out.append(fn)
    return out


def load_autonames(path, seen_fn, seen_obj, manifest, matched):
    """Mechanically derived names (abi/autonames.py) good enough to link against.

    The same rule as the matches: a hand-written entry wins, but a contradiction
    is an error rather than a silent overwrite, because one of the two is wrong.
    """
    if not os.path.exists(path):
        return []
    with open(path) as f:
        an = yaml.safe_load(f)
    by_name = {fn["name"]: fn["address"] & ~1 for fn in manifest["functions"]}
    by_name.update({fn["name"]: fn["address"] & ~1 for fn in matched})
    # A manifest entry may take an address abi/autonames.py also names, and says
    # which derived name it takes it from, so the claim only holds against the
    # derivation it was made against: if a later run derives something else
    # there, the refusal below fires instead of the stale claim winning.
    superseded = {fn["supersedes"]: fn["address"] & ~1
                  for fn in manifest["functions"] if "supersedes" in fn}
    out, names = [], set()
    for fn in an.get("functions", []):
        if not IDENT.match(fn["name"]):
            raise ManifestError("autonames.yaml name %r is not an identifier" % fn["name"])
        addr = fn["address"] & ~1
        if fn["name"] in by_name:
            if by_name[fn["name"]] != addr:
                raise ManifestError(
                    "autonames.yaml puts %s at 0x%x, the manifest at 0x%x"
                    % (fn["name"], addr, by_name[fn["name"]]))
            continue
        if superseded.get(fn["name"]) == addr:
            continue
        if fn["name"] in names:
            raise ManifestError("autonames.yaml repeats the name %s" % fn["name"])
        for seen, what in ((seen_fn, "a function"), (seen_obj, "data")):
            if addr in seen:
                raise ManifestError(
                    "autonames.yaml names 0x%x %s, the manifest already has %s %s there"
                    % (addr, fn["name"], what, seen[addr]))
        names.add(fn["name"])
        out.append(fn)
    return out


def main():
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--manifest", default=os.path.join(here, "hwa10.yaml"))
    ap.add_argument("--out", default=os.path.join(here, "out"))
    ap.add_argument("--matches", default=os.path.join(here, "matches.yaml"))
    ap.add_argument("--autonames", default=os.path.join(here, "autonames.yaml"))
    ap.add_argument("--match-threshold", type=float, default=0.80,
                    help="score a match must reach to enter the manifest")
    args = ap.parse_args()

    with open(args.manifest) as f:
        m = yaml.safe_load(f)

    structs = {s["name"]: s for s in m.get("structs", []) + m.get("table_structs", [])}
    # A function-pointer type is the only thing a table's handler field can be
    # declared as, and it is what fixes the handler prototype for everything the
    # table dispatches; the manifest names them so a struct field can use one.
    typedefs = {t["name"]: t for t in m.get("typedefs", [])}
    seen_fn, seen_obj = {}, {}
    h = ["/* Generated by abi/gen.py from hwa10.yaml -- do not edit. */",
         "#ifndef HWA10_H", "#define HWA10_H", ""]
    ld = ["/* Generated by abi/gen.py from hwa10.yaml -- do not edit. */", ""]

    codec_sizers = {fn["name"][len("wpp_obj_"):] for fn in m["functions"]
                    if fn["name"].startswith("wpp_obj_") and fn["name"].endswith("_size")}

    order, emitted = [], set()

    def emit_struct(name):
        if name in emitted:
            return
        emitted.add(name)
        s = structs[name]
        body = ["struct %s {" % name]
        for ftype, fname in s["fields"]:
            body.append("    %s;" % decl(ftype, fname, structs, typedefs))
        body.append("};")
        order.append("\n".join(body))

    # An enum is a set of numbers the image keys on; declaring them as an enum
    # rather than as loose constants is what lets a replacement switch on one.
    for e in m.get("enums", []):
        if e.get("notes"):
            h.append("/* %s */" % " ".join(e["notes"].split()))
        h.append("enum %s {" % e["name"])
        for v in e["values"]:
            if v.get("notes"):
                h.append("    /* %s */" % " ".join(v["notes"].split()))
            h.append("    %s = 0x%x," % (v["name"], v["value"]))
        h.append("};")
    h.append("")

    for t in typedefs.values():
        ret, suffix = c_type(t["ret"], structs, typedefs)
        if suffix:
            raise ManifestError("typedef %s returns an array" % t["name"])
        params = [decl(a, n, structs, typedefs) for a, n in t.get("args", [])] or ["void"]
        if t.get("notes"):
            h.append("/* %s */" % " ".join(t["notes"].split()))
        sep = "" if ret.endswith("*") else " "
        h.append("typedef %s%s(*%s)(%s);" % (ret, sep, t["name"], ", ".join(params)))
    h.append("")
    for name in structs:
        emit_struct(name)
    h += order + [""]

    h.append("/* functions */")
    for fn in m["functions"]:
        addr = fn["address"] & ~1
        check_unique(seen_fn, addr, fn["name"], "function")
        params = [decl(t, n, structs, typedefs) for t, n in fn.get("args", [])]
        if fn.get("variadic"):
            params.append("...")
        elif not params:
            params = ["void"]
        ret, suffix = c_type(fn["ret"], structs, typedefs)
        if suffix:
            raise ManifestError("%s returns an array" % fn["name"])
        sep = "" if ret.endswith("*") else " "
        if fn.get("notes"):
            h.append("/* %s */" % " ".join(fn["notes"].split()))
        h.append("extern %s%s%s(%s);" % (ret, sep, fn["name"], ", ".join(params)))
        ld.append("PROVIDE(%s = 0x%x | 1);" % (fn["name"], addr))
    h.append("")

    h.append("/* globals */")
    ld.append("")
    for g in m["globals"]:
        check_unique(seen_obj, g["address"], g["name"], "global")
        if g.get("notes"):
            h.append("/* %s */" % " ".join(g["notes"].split()))
        h.append("extern %s;" % decl(g["type"], g["name"], structs, typedefs))
        ld.append("PROVIDE(%s = 0x%x);" % (g["name"], g["address"]))
    h.append("")

    h.append("/* tables */")
    ld.append("")
    for t in m["tables"]:
        if t["entry"] not in structs:
            raise ManifestError("table %s uses unknown entry struct %r" % (t["name"], t["entry"]))
        check_unique(seen_obj, t["address"], t["name"], "table")
        h.append("extern struct %s %s[%d]; /* stride %d */"
                 % (t["entry"], t["name"], t["count"], t["stride"]))
        ld.append("PROVIDE(%s = 0x%x);" % (t["name"], t["address"]))
    # The send path is generic (wpp_obj_encoder erases the object type), so
    # without this every reply site would cast its own encoder back to it. A
    # class with both a sizer and an encoder in the manifest has its pair fixed
    # by the image, and the cast belongs here once rather than at each site.
    send_pairs = []
    for fn in m["functions"]:
        mm = re.match(r"^wpp_obj_(.+)_encode$", fn["name"])
        if not mm or mm.group(1) + "_size" not in codec_sizers:
            continue
        arg = fn.get("args", [])
        if len(arg) != 2 or not arg[0][0].startswith("const struct wpp_"):
            continue
        send_pairs.append((mm.group(1), arg[0][0]))
    h += ["", "/* typed send path: one inline wrapper per object type the image both",
          "   sizes and encodes, over wpp_send_object_alias's erased pair. */"]
    for rust, ptype in send_pairs:
        h.append("static inline void wpp_send_%s(unsigned short command, %sobj)"
                 % (rust, ptype if ptype.endswith("*") else ptype + " "))
        h.append("{")
        h.append("    wpp_send_object_alias(obj, command, wpp_obj_%s_size," % rust)
        h.append("                          (wpp_obj_encoder)wpp_obj_%s_encode);" % rust)
        h.append("}")

    h += ["", "/* matched library functions -- abi/matches.yaml, from abi/match.py.",
          "   Their prototypes are the SDK's own, so they need the SDK types; the",
          "   linker script provides every address either way. */",
          "#ifdef HWA10_MATCHED_PROTOTYPES"]
    ld += ["", "/* matched library functions (abi/matches.yaml) */"]
    matched = load_matches(args.matches, args.match_threshold, seen_fn, seen_obj, m)
    for fn in matched:
        check_unique(seen_fn, fn["address"], fn["name"], "matched function")
        ld.append("PROVIDE(%s = 0x%x | 1);" % (fn["name"], fn["address"]))
        if fn.get("proto"):
            h.append("/* %s */" % fn["header"])
            h.append("extern %s" % fn["proto"])
    h += ["", "/* mechanically derived names -- abi/autonames.yaml, from",
          "   abi/autonames.py. Only the classes whose reference supplies a real",
          "   header get a prototype; the rest are addresses the linker can bind. */"]
    ld += ["", "/* derived names (abi/autonames.yaml) */"]
    for fn in load_autonames(args.autonames, seen_fn, seen_obj, m, matched):
        check_unique(seen_fn, fn["address"], fn["name"], "derived function")
        ld.append("PROVIDE(%s = 0x%x | 1);" % (fn["name"], fn["address"]))
        if fn.get("proto"):
            h.append("/* %s */" % fn.get("header", fn.get("proto_derived", fn["class"])))
            h.append("extern %s" % fn["proto"])
    h += ["#endif", "", "#endif"]

    os.makedirs(args.out, exist_ok=True)
    for name, lines in (("hwa10.h", h), ("hwa10.ld", ld)):
        with open(os.path.join(args.out, name), "w") as f:
            f.write("\n".join(lines) + "\n")
        print(os.path.join(args.out, name))


if __name__ == "__main__":
    try:
        main()
    except ManifestError as e:
        sys.exit("abi/gen.py: %s" % e)
