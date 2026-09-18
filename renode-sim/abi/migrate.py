#!/usr/bin/env python3
"""Turn the single manifest into hand C headers, one address map and one facts file.

    python3 abi/migrate.py

Reads the old inputs -- abi/hwa10.yaml, abi/matches.yaml, abi/autonames.yaml,
renode-sim/symbols.txt and the partition's abi/out/ghidra/modules.json -- and
writes:

  abi/include/withings/<module>.h   what a thing is: prototypes, structs, enums,
                                    globals and table declarations, no addresses
  abi/include/withings/all.h        the umbrella a consumer includes
  abi/symbols.yaml                  where a thing is: one entry per address
  abi/facts.yaml                    what is neither C nor an address

The split is the point. A prototype is C and belongs in a file a C compiler
reads; an address is a measurement of this image and belongs in a file the
tools read; a region argued for in prose is neither. The converter exists so
the split can be re-derived after the naming tools land more names, which is
why it is mechanical: the module a symbol lands in comes from its name, from
the manifest's own section headings and from the partition's module map, in
that order, and never from a list kept here by hand.
"""

import collections
import json
import os
import re
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
INCLUDE = os.path.join(HERE, "include", "withings")

# A C identifier. A match like "prvInitialiseNewTask.isra.0" is a GCC clone of
# a static and is not one, which is fine in the map -- the map is not C -- but
# a derivation that produces one has a bug, because a derived name is meant to
# be linkable.
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

SYM_LINE = re.compile(r"^(0x[0-9a-fA-F]+)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(.*)$")


# ------------------------------------------------------------------ C types

# The manifest's own scalar spelling. This table is the last thing that reads
# it: past the conversion the headers are C and the compiler is what parses
# them.
SCALARS = {
    "void": "void", "bool": "int", "i8": "signed char", "u8": "unsigned char",
    "i16": "short", "u16": "unsigned short", "i32": "int", "u32": "unsigned int",
    "i64": "long long", "u64": "unsigned long long", "char": "char",
    "float": "float", "f64": "double",
}

ARRAY = re.compile(r"^(?P<base>[A-Za-z_][A-Za-z0-9_ *]*?)\[(?P<n>0x[0-9a-fA-F]+|\d+)\]$")


class MigrateError(Exception):
    pass


def c_type(t, structs, typedefs):
    """(base text, array suffix) for one manifest type."""
    t = str(t).strip()
    m = ARRAY.match(t)
    if m:
        base, inner = c_type(m.group("base"), structs, typedefs)
        if inner:
            raise MigrateError("nested array type %r" % t)
        return base, "[%s]" % m.group("n")
    stars = ""
    while t.endswith("*"):
        stars = " *" + stars.lstrip()
        t = t[:-1].strip()
    const = ""
    if t.startswith("const "):
        const, t = "const ", t[len("const "):].strip()
    if t.startswith("struct "):
        name = t[len("struct "):].strip()
        if name not in structs:
            raise MigrateError("unknown struct %r" % t)
        base = "struct %s" % name
    elif t in SCALARS:
        base = SCALARS[t]
    elif t in typedefs:
        base = t
    else:
        raise MigrateError("unknown type %r" % t)
    return const + base + stars, ""


def decl(t, name, structs, typedefs):
    base, suffix = c_type(t, structs, typedefs)
    sep = "" if base.endswith("*") else " "
    return "%s%s%s%s" % (base, sep, name, suffix)


def referenced_structs(t):
    """Every struct name a manifest type mentions."""
    return set(re.findall(r"struct\s+([A-Za-z_][A-Za-z0-9_]*)", str(t)))


def by_value(t):
    """True where the type embeds a struct rather than pointing at one.

    A pointer needs a forward declaration; a member or an array of members
    needs the definition, so it decides whether the header includes another.
    """
    t = str(t)
    return "struct " in t and "*" not in t


# ------------------------------------------------------------------- modules

# The header a name belongs to, longest prefix first. A name is the strongest
# signal there is: it was chosen for the thing, while a module tag is the tag
# of the log line the body happens to reach.
NAME_PREFIXES = [
    ("wpp_", "wpp"), ("shell_", "shell"), ("dblib_", "dblib"),
    ("DBLIB_", "dblib"),
    ("sensors_sync_", "sensors_sync"), ("sensor_", "sensors_sync"),
    ("ppg_", "sensors_sync"),
    ("ecg_", "ecg"), ("algo_", "ecg"), ("qrs_", "ecg"), ("afib_", "ecg"),
    ("hr_", "hr"),
    ("wui_", "wui"), ("font_", "wui"), ("asset_", "wui"), ("rre_", "wui"),
    ("glyph_", "wui"), ("screen_", "wui"),
    ("battery_", "bat"), ("bq25180_", "bat"), ("charger_", "bat"),
    ("fwblk_", "flash"), ("flash_", "flash"), ("int_flash_", "flash"),
    ("spi_flash_", "flash"), ("mx25r_", "flash"),
    ("crown_", "crown"),
    ("sd_", "ble"), ("ble_", "ble"), ("gap_", "ble"), ("gatt", "ble"),
    ("nrf_nvic", "ble"),
    ("wlog", "wlog"),
    ("adxl367_", "sensors"), ("max86173_", "sensors"), ("apds", "sensors"),
    ("tmp117_", "sensors"), ("ads1115_", "sensors"),
    ("i2c_", "hw"), ("spi_", "hw"), ("gpio_", "hw"), ("saadc_", "hw"),
    ("rambkp", "boot"), ("boot_", "boot"), ("appl_", "boot"),
]

# A FreeRTOS body is named by the kernel's own convention, not by a prefix of
# ours; the convention is what identifies it.
FREERTOS = re.compile(r"^(x|v|ul|uc|pv|prv|pd)[A-Z]")

# The partition's module tag, folded onto a header. A tag not named here is not
# guessed at: its symbol goes to misc.h, where a hand can move it.
MODULE_HEADER = {
    "WPP": "wpp", "WPPS": "wpp", "WPP_LITE": "wpp", "WPP_V2": "wpp",
    "WPP_V3": "wpp", "WPP_CLIENT": "wpp", "WPP_LITE_CLIENT": "wpp",
    "BLE_WPP": "wpp", "BLE_WPPS": "wpp", "BLE_WPP_C": "wpp",
    "SHELL": "shell",
    "DBLIB": "dblib", "DBLIB_PORT": "dblib", "INITDBLIB": "dblib",
    "SENSORS_SYNC": "sensors_sync", "ACQ": "sensors_sync",
    "MEASURE_LIVE": "sensors_sync", "RAW": "sensors_sync",
    "RAW_DATA_MODE": "sensors_sync", "RAW_DATA_SAVE": "sensors_sync",
    "ECG": "ecg", "ECG_PPG": "ecg", "QRS_FEATURES": "ecg",
    "PPG_AFIB": "ecg", "PPG_AFIB_STORE": "ecg", "ALGO_MANAGER": "ecg",
    "HR": "hr", "HR_MEASURE": "hr", "HR_EVENT": "hr", "HR_COMPRESSION": "hr",
    "SPO2": "hr",
    "WUI": "wui", "GUI": "wui", "GUIN": "wui", "UI": "wui",
    "WAM_SCREEN": "wui", "WAM_GUI_RESOURCE": "wui", "RRE_FONTS": "wui",
    "GLYPH_CACHE": "wui", "HOME_FACE": "wui",
    "BAT": "bat", "BATTERY": "bat", "BQ25180": "bat", "PWR": "bat",
    "FWBLK": "flash", "MCUFLASH": "flash", "FLASH_CACHE": "flash",
    "WFTL": "flash", "WFTL_UP": "flash", "UPDATE": "flash",
    "DIGITAL_CROWN": "crown",
    "BLE": "ble", "GAP": "ble", "GATTC": "ble", "ADV": "ble", "SD": "ble",
    "SOFTDEVICE": "ble", "SOFTDEVICE_BLE_CORE": "ble", "CONN_PARAM": "ble",
    "BLUETOOTH": "ble", "BT": "ble", "PAIRING": "ble", "PAIRING_PROTO": "ble",
    "WLOG": "wlog", "CONS_LOG": "wlog",
    "ADXL": "sensors", "ADXL367": "sensors", "MAX86173": "sensors",
    "MAX8617X": "sensors", "APDS": "sensors", "TMP117": "sensors",
    "LIGHT_SENSOR": "sensors", "TEMPERATURE": "sensors", "BODY_TEMP": "sensors",
    "I2C": "hw", "I2C_BUS": "hw", "SPI": "hw", "SPIM": "hw", "ADC": "hw",
    "BOARD": "hw", "CLOCK": "hw", "LRA": "hw", "VIB": "hw", "STEP_MOTOR": "hw",
    "INIT": "boot", "FACTORY": "boot", "FACTORY_STATE": "boot",
}

# The manifest's own section headings, which are a hand grouping and the last
# word where a name says nothing.
HEADING_HEADER = {
    "FreeRTOS": "freertos",
    "logging": "wlog",
    "WPP / BLE": "wpp",
    "WPP wire codec primitives": "wpp_objects",
    "battery / charger": "bat",
    "fwblk (external-flash firmware bank table)": "flash",
    "crown": "crown",
    "WPP dispatch and framing": "wpp",
    "WPP object codecs": "wpp_objects",
    "dblib, the settings store on the external MX25R": "dblib",
    "roles read off the bodies of the modules that carry no log tag": "misc",
    "the ECG algorithm library, from a pipe-driven acquisition": "ecg",
    "WPP parsers the object walk reaches by a direct call": "wpp_objects",
    "WPP reply sizers": "wpp_objects",
    "library bodies the partition or the name derivation got wrong": "freertos",
    "the sensor-sync path": "sensors_sync",
    "the ECG session, and the HR algorithm's two ends": "ecg",
    "the sensor-sync path: one 0x4c-byte slot shared by two streams": "sensors_sync",
    "WPP objects, in memory": "wpp_objects",
    "dblib entry layouts": "dblib",
}

GENERATED = "wpp_objects"

# The generated header's inline send wrappers call the erased send path and cast
# to its two callback types, all three of which are hand-declared next door.

# The wire objects: a codec body abi/protocol.py recovered, and the struct it
# reads or writes. They are the generated header, and nothing else is.
CODEC = re.compile(r"^wpp_obj_.+_(encode|parse|size)(_copy_.*)?$")
WIRE_STRUCT = re.compile(r"^wpp_[A-Z]")

# A parser a handler calls directly is not reached through the callback
# register abi/protocol.py scans, so it was read off the inlined walk by hand
# and is hand-maintained however much its name looks generated. The heading it
# was written under is what says so.
HAND_WIRE = {"WPP parsers the object walk reaches by a direct call": "wpp"}


def header_of(name, address, modules, heading):
    if heading in HAND_WIRE:
        return HAND_WIRE[heading]
    if CODEC.match(name) or WIRE_STRUCT.match(name):
        return GENERATED
    for prefix, module in NAME_PREFIXES:
        if name.startswith(prefix):
            return module
    if FREERTOS.match(name):
        return "freertos"
    tag = modules.get(address)
    if tag and tag in MODULE_HEADER:
        return MODULE_HEADER[tag]
    if heading in HEADING_HEADER:
        return HEADING_HEADER[heading]
    return "misc"


# ------------------------------------------------- the manifest's own headings

HEADING = re.compile(r"^\s*#\s*-{2,}\s*(.*?)\s*-{2,}\s*$")
TOPLEVEL = re.compile(r"^([a-z_]+):\s*$")
ENTRY = re.compile(r"^\s*-\s+(name|addr):\s*(\S+)")


def headings(path):
    """{(section, entry name): heading} for the manifest's `# --- x ---` lines.

    The headings are a hand grouping of the entries under them and the only
    grouping the manifest carries, so they are read out of the text rather
    than lost with the comments YAML drops.
    """
    out, section, heading = {}, None, None
    for line in open(path):
        m = TOPLEVEL.match(line)
        if m:
            section, heading = m.group(1), None
            continue
        m = HEADING.match(line)
        if m:
            heading = m.group(1)
            continue
        m = ENTRY.match(line)
        if m and section:
            out.setdefault((section, m.group(2).strip("'\"")), heading)
    return out


# -------------------------------------------------------------------- output

def wrap(text, width=76, indent=""):
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return [indent + l for l in lines]


def comment(notes, indent=""):
    if not notes:
        return []
    lines = wrap(notes, 74 - len(indent))
    if len(lines) == 1:
        return ["%s/* %s */" % (indent, lines[0])]
    return ([indent + "/* " + lines[0]] +
            ["%s   %s" % (indent, l) for l in lines[1:]] + [indent + "   */"])


class Header(object):
    def __init__(self, name):
        self.name = name
        self.structs = []       # struct names defined here, in order
        self.typedefs = []
        self.enums = []
        self.functions = []
        self.globals = []
        self.tables = []


def render(header, m, structs, typedefs, enums, owner, td_owner):
    """One module header: C only, and no address anywhere in it."""
    guard = "WITHINGS_%s_H" % header.name.upper()
    out = ["/* HWA10 (ScanWatch 2) application firmware v3411: the %s module's ABI."
           % header.name,
           " *",
           " * What each of these is, not where it is: abi/symbols.yaml holds the",
           " * addresses. Written by abi/migrate.py from the old single manifest and",
           " * hand-maintained since. */",
           "#ifndef %s" % guard, "#define %s" % guard, ""]

    # A definition this header needs by value comes from another header; a
    # pointer only needs the tag, which is declared rather than included so no
    # two headers can need each other.
    needed, forward, include = set(), set(), set()

    def note(t):
        for s in referenced_structs(t):
            if owner.get(s) == header.name:
                continue
            (needed if by_value(t) else forward).add(s)
        # A typedef has no incomplete form: a header that spells one has to see
        # the definition, so it includes the header that holds it.
        for name in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", str(t)):
            if name in typedefs and td_owner[name] != header.name:
                include.add(td_owner[name])

    for name in header.structs:
        for ftype, _ in structs[name]["fields"]:
            note(ftype)
    for fn in header.functions:
        note(fn["ret"])
        for t, _ in fn.get("args", []):
            note(t)
    for t in header.typedefs:
        note(typedefs[t]["ret"])
        for a, _ in typedefs[t].get("args", []):
            note(a)
    for g in header.globals:
        note(g["type"])
    for t in header.tables:
        needed.add(t["entry"])

    include |= {owner[s] for s in needed if owner.get(s) != header.name}
    includes = sorted(include)
    for inc in includes:
        out.append('#include "withings/%s.h"' % inc)
    if includes:
        out.append("")
    forward -= {s for s in needed}
    for s in sorted(forward):
        if owner.get(s) not in includes:
            out.append("struct %s;" % s)
    if forward:
        out.append("")

    for e in header.enums:
        out += comment(e.get("notes"))
        out.append("enum %s {" % e["name"])
        for v in e["values"]:
            out += comment(v.get("notes"), "    ")
            out.append("    %s = 0x%x," % (v["name"], v["value"]))
        out += ["};", ""]

    for name in header.typedefs:
        t = typedefs[name]
        ret, suffix = c_type(t["ret"], structs, typedefs)
        if suffix:
            raise MigrateError("typedef %s returns an array" % name)
        params = [decl(a, n, structs, typedefs) for a, n in t.get("args", [])] or ["void"]
        out += comment(t.get("notes"))
        sep = "" if ret.endswith("*") else " "
        out.append("typedef %s%s(*%s)(%s);" % (ret, sep, name, ", ".join(params)))
    if header.typedefs:
        out.append("")

    for name in header.structs:
        s = structs[name]
        out += comment(s.get("notes"))
        out.append("struct %s {" % name)
        for ftype, fname in s["fields"]:
            out.append("    %s;" % decl(ftype, fname, structs, typedefs))
        out += ["};", ""]

    if header.functions:
        out.append("/* functions */")
        for fn in header.functions:
            params = [decl(t, n, structs, typedefs) for t, n in fn.get("args", [])]
            if fn.get("variadic"):
                params.append("...")
            elif not params:
                params = ["void"]
            ret, suffix = c_type(fn["ret"], structs, typedefs)
            if suffix:
                raise MigrateError("%s returns an array" % fn["name"])
            sep = "" if ret.endswith("*") else " "
            out += comment(fn.get("notes"))
            out.append("extern %s%s%s(%s);" % (ret, sep, fn["name"], ", ".join(params)))
        out.append("")

    if header.globals:
        out.append("/* globals */")
        for g in header.globals:
            out += comment(g.get("notes"))
            out.append("extern %s;" % decl(g["type"], g["name"], structs, typedefs))
        out.append("")

    if header.tables:
        out.append("/* tables */")
        for t in header.tables:
            out += comment(t.get("notes"))
            out.append("extern struct %s %s[%d];" % (t["entry"], t["name"], t["count"]))
        out.append("")

    out.append("#endif")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------ the address map

def hexed(v):
    return "0x%x" % v


class Hex(int):
    """An address, written the way every other address in the repo is."""


def _hex(dumper, value):
    return dumper.represent_scalar("tag:yaml.org,2002:int", "0x%x" % value)


yaml.add_representer(Hex, _hex)

ORDER = ["address", "name", "aliases", "kind", "class", "module", "corrects",
         "supersedes", "evidence"]


def dump_symbols(entries, path):
    """abi/symbols.yaml, in address order."""
    rows = []
    for e in entries:
        row = collections.OrderedDict()
        for key in ORDER:
            if key in e:
                row[key] = Hex(e[key]) if key in ("address", "corrects") else e[key]
        rows.append(row)
    yaml.add_representer(
        collections.OrderedDict,
        lambda d, v: d.represent_mapping("tag:yaml.org,2002:map", v.items()))
    with open(path, "w") as fh:
        fh.write(
            "# HWA10 (ScanWatch 2) application firmware v3411 -- the address map.\n"
            "#\n"
            "# Where a thing is. What it is lives in abi/include/withings/*.h (C) and\n"
            "# abi/facts.yaml (everything that is neither C nor an address).\n"
            "#\n"
            "# Written by abi/migrate.py from abi/hwa10.yaml, abi/matches.yaml,\n"
            "# abi/autonames.yaml and symbols.txt. `class: hand` entries are edited\n"
            "# here; the derived classes are refreshed by re-running the converter.\n"
            "# A hand entry that overrides a derivation names what it overrides in\n"
            "# `corrects` (the address a body match landed on) or `supersedes` (the\n"
            "# name a derivation produced), so the claim only holds against the\n"
            "# measurement it was made against and the refusal fires again when that\n"
            "# measurement moves instead of the stale claim winning silently.\n\n")
        yaml.dump({"symbols": rows}, fh, sort_keys=False, width=78,
                  default_flow_style=False)


# ----------------------------------------------------------------------- main

def main():
    manifest_path = os.path.join(HERE, "hwa10.yaml")
    m = yaml.safe_load(open(manifest_path))
    heads = headings(manifest_path)

    structs = {s["name"]: s for s in m["structs"] + m["table_structs"]}
    typedefs = {t["name"]: t for t in m["typedefs"]}
    enums = {e["name"]: e for e in m["enums"]}

    modules = {}
    modpath = os.path.join(HERE, "out", "ghidra", "modules.json")
    if os.path.exists(modpath):
        modules = {int(a, 16): v["module"]
                   for a, v in json.load(open(modpath))["functions"].items()}

    headers = collections.OrderedDict()

    def header(name):
        return headers.setdefault(name, Header(name))

    # 1. Functions, globals and tables land by name, tag and heading.
    symbols = []
    for fn in m["functions"]:
        addr = fn["address"] & ~1
        h = header_of(fn["name"], addr, modules, heads.get(("functions", fn["name"])))
        header(h).functions.append(fn)
        e = {"address": addr, "name": fn["name"], "kind": "function",
             "class": "hand", "module": h}
        if "corrects" in fn:
            e["corrects"] = fn["corrects"] & ~1
        if "supersedes" in fn:
            e["supersedes"] = fn["supersedes"]
        if fn.get("notes"):
            e["evidence"] = " ".join(str(fn["notes"]).split())
        symbols.append(e)
    for g in m["globals"]:
        h = header_of(g["name"], g["address"], modules, heads.get(("globals", g["name"])))
        header(h).globals.append(g)
        e = {"address": g["address"], "name": g["name"], "kind": "global",
             "class": "hand", "module": h}
        if g.get("notes"):
            e["evidence"] = " ".join(str(g["notes"]).split())
        symbols.append(e)
    for t in m["tables"]:
        h = header_of(t["name"], t["address"], modules, heads.get(("tables", t["name"])))
        header(h).tables.append(t)
        e = {"address": t["address"], "name": t["name"], "kind": "table",
             "class": "hand", "module": h}
        if t.get("notes"):
            e["evidence"] = " ".join(str(t["notes"]).split())
        symbols.append(e)

    # 2. A struct, typedef or enum belongs to the header of whatever uses it;
    #    a type nothing in the map uses is placed by its own name.
    owner = {}
    users = collections.defaultdict(list)
    for h in headers.values():
        for fn in h.functions:
            for t in [fn["ret"]] + [a for a, _ in fn.get("args", [])]:
                for s in referenced_structs(t):
                    users[s].append(h.name)
        for g in h.globals:
            for s in referenced_structs(g["type"]):
                users[s].append(h.name)
        for t in h.tables:
            users[t["entry"]].append(h.name)
    for name in structs:
        # A wire object is the generated header's whatever else reads it: the
        # codec that writes it is what recovered the shape, and a struct
        # defined next to its one hand user would put half the wire in bat.h.
        if WIRE_STRUCT.match(name):
            owner[name] = header_of(name, 0, modules,
                                    heads.get(("structs", name)))
            continue
        cand = users.get(name)
        owner[name] = (collections.Counter(cand).most_common(1)[0][0] if cand
                       else header_of(name, 0, modules, None))
    # A struct a placed struct embeds or points at follows it, so no header
    # holds half a shape.
    changed = True
    while changed:
        changed = False
        for name, s in structs.items():
            for ftype, _ in s["fields"]:
                for dep in referenced_structs(ftype):
                    if by_value(ftype) and owner[dep] != owner[name]:
                        owner[dep] = owner[name]
                        changed = True
    for name in sorted(structs):
        header(owner[name])

    # Definition before use inside a header: a by-value member has to be
    # complete where it is written.
    placed = set()

    def place(name):
        if name in placed:
            return
        placed.add(name)
        for ftype, _ in structs[name]["fields"]:
            if by_value(ftype):
                for dep in referenced_structs(ftype):
                    place(dep)
        headers[owner[name]].structs.append(name)

    for name in structs:
        place(name)

    # A function-pointer typedef belongs where it is spelled most: the row
    # struct that declares the slot and the prototypes that take it.
    td_users = collections.defaultdict(list)
    for h in headers.values():
        spelled = ([f[0] for s in h.structs for f in structs[s]["fields"]] +
                   [fn["ret"] for fn in h.functions] +
                   [a for fn in h.functions for a, _ in fn.get("args", [])] +
                   [g["type"] for g in h.globals])
        for t in spelled:
            for name in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", str(t)):
                if name in typedefs:
                    td_users[name].append(h.name)
    td_owner = {}
    for name in typedefs:
        cand = td_users.get(name)
        td_owner[name] = (collections.Counter(cand).most_common(1)[0][0] if cand
                          else header_of(name, 0, modules, None))
        header(td_owner[name]).typedefs.append(name)
    for name, e in enums.items():
        header(header_of(name, 0, modules,
                         heads.get(("enums", name)))).enums.append(e)

    # 3. The derived names. A match or a derivation contradicting a hand entry
    #    is an error rather than a second entry: one of the two is wrong.
    by_name = {e["name"]: e["address"] for e in symbols}
    by_addr = {e["address"]: e for e in symbols}
    corrected = {e["name"]: e["corrects"] for e in symbols if "corrects" in e}
    superseded = {e["supersedes"]: e["address"] for e in symbols if "supersedes" in e}

    def fold(doc, klass, require_identifier):
        for fn in doc.get("functions", []):
            if require_identifier and not IDENT.match(fn["name"]):
                raise MigrateError("%s name %r is not an identifier"
                                   % (klass, fn["name"]))
            addr = fn["address"] & ~1
            if fn["name"] in by_name:
                if corrected.get(fn["name"]) == addr:
                    continue
                if by_name[fn["name"]] != addr:
                    raise MigrateError("%s puts %s at 0x%x, the hand map at 0x%x"
                                       % (klass, fn["name"], addr, by_name[fn["name"]]))
                continue
            if superseded.get(fn["name"]) == addr:
                continue
            if addr in by_addr:
                raise MigrateError("%s names 0x%x %s, the map already has %s there"
                                   % (klass, addr, fn["name"], by_addr[addr]["name"]))
            e = {"address": addr, "name": fn["name"], "kind": "function",
                 "class": fn.get("class", klass)}
            evidence = fn.get("evidence")
            if evidence is None and "score" in fn:
                evidence = ("%s, score %.2f over %d insns, %s"
                            % (fn["variant"], fn["score"], fn["insns"], fn["how"]))
            if evidence:
                e["evidence"] = " ".join(str(evidence).split())
            symbols.append(e)
            by_name[fn["name"]] = addr
            by_addr[addr] = e

    fold(yaml.safe_load(open(os.path.join(HERE, "matches.yaml"))), "match", False)
    fold(yaml.safe_load(open(os.path.join(HERE, "autonames.yaml"))), "derived", True)

    # 4. symbols.txt's hand facts. It mixes code, flash data and RAM in one
    #    list and says which is which nowhere, so its entries keep no kind: the
    #    analysis is what decides whether a function starts there.
    for line in open(os.path.join(SIM, "symbols.txt")):
        line = line.strip()
        if line.startswith("#") or not line:
            continue
        mm = SYM_LINE.match(line)
        if not mm:
            continue
        addr = int(mm.group(1), 16) & ~1
        if mm.group(2) in by_name:
            continue
        if addr in by_addr:
            # The prose map calls wpp_cmd_table the master command table and
            # 0x73e3c crit_enter; both are true of the same address, so the
            # second name is an alias rather than a second entry.
            by_addr[addr].setdefault("aliases", []).append(mm.group(2))
            by_name[mm.group(2)] = addr
            continue
        e = {"address": addr, "name": mm.group(2), "kind": "label",
             "class": "prose"}
        if mm.group(3):
            e["evidence"] = " ".join(mm.group(3).split())
        symbols.append(e)
        by_addr[addr] = e
        by_name[mm.group(2)] = addr

    symbols.sort(key=lambda e: (e["address"], e["name"]))
    dump_symbols(symbols, os.path.join(HERE, "symbols.yaml"))

    # 5. What is neither C nor an address.
    facts = {
        "meta": m["meta"],
        "regions": m["regions"],
        "fixed_points": [
            {"addr": int(str(f["addr"]), 0), "name": f["name"],
             "read_by": f["read_by"], "notes": f["notes"]}
            for f in m["fixed_points"]],
        "reference_build": {
            "tick_timer": {
                "header": "CMSIS/nrf52/portmacro_cmsis.h",
                "replace": [["NRF_RTC1", "NRF_RTC2"], ["RTC1_IRQn", "RTC2_IRQn"]],
                "notes": (
                    "The SDK's FreeRTOS port hardcodes RTC1 as the tick source."
                    " This firmware's tick is RTC2: 0x74034 writes RTC2's"
                    " PRESCALER as 0x20 and vector 52, RTC2's, is the tick ISR."
                    " The path is relative to the SDK's freertos/portable."),
            },
        },
    }
    with open(os.path.join(HERE, "facts.yaml"), "w") as fh:
        fh.write("# HWA10 (ScanWatch 2) application firmware v3411 -- what is neither\n"
                 "# C nor an address: the regions the relink places into, the sections\n"
                 "# no layout may move, and the corrections the reference build needs.\n"
                 "# Written by abi/migrate.py; hand-maintained.\n\n")
        yaml.dump(facts, fh, sort_keys=False, width=78, default_flow_style=False)

    # 6. The headers.
    os.makedirs(INCLUDE, exist_ok=True)
    written = sorted(headers)
    for name in written:
        # abi/protocol.py writes the wire: the layouts and the codecs over them
        # are read off the image's own bodies, so a converter that wrote them
        # too would be a second author of one file.
        if name == GENERATED:
            continue
        path = os.path.join(INCLUDE, name + ".h")
        with open(path, "w") as fh:
            fh.write(render(headers[name], m, structs, typedefs, enums, owner,
                            td_owner))
    with open(os.path.join(INCLUDE, "all.h"), "w") as fh:
        fh.write("/* Every module of the HWA10 application ABI.\n"
                 " *\n"
                 " * Written by abi/migrate.py. A consumer that wants one module"
                 " includes it\n * directly; this is for the generated sources,"
                 " which cut across all of them. */\n"
                 "#ifndef WITHINGS_ALL_H\n#define WITHINGS_ALL_H\n\n")
        for name in written:
            fh.write('#include "withings/%s.h"\n' % name)
        fh.write("\n#endif\n")

    print("%d symbols -> abi/symbols.yaml" % len(symbols))
    print("%d headers -> abi/include/withings/: %s"
          % (len(written) - 1, " ".join(n for n in written if n != GENERATED)))
    print("%s.h is abi/protocol.py's: python3 abi/protocol.py --emit-header"
          % GENERATED)


if __name__ == "__main__":
    try:
        main()
    except MigrateError as e:
        sys.exit("abi/migrate.py: %s" % e)
