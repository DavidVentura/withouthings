#!/usr/bin/env python3
"""The RAM globals, named from what the image establishes about each word.

    python3 abi/globals.py             # writes the global class of abi/symbols.yaml
    python3 abi/globals.py --report    # what each word is named from; writes nothing

abi/symbols.yaml names RAM only where the FreeRTOS creates, the typed headers
and the stores reach, which is 80 of the 1517 words the application's own code
addresses through its literal pools. Everything downstream of the map stops
there: a word getter is eight bytes of `ldr`/`bx lr` and says nothing about
itself, and the same is true of the 439 bodies of that size.

What the image does establish about a word, strongest reading first:

  the settings cache    a dblib-backed setting is a cache word plus an
                        accessor pair -- a reader that copies the word out and
                        a writer that compares, stores and persists it with
                        `dblib_set(id, &cache, len)`. The id is an immediate at
                        the persist call, so the calling sequence names the
                        word, and abi/include/withings/dblib.h names the id.
  the debug dump        the RAM item table's rows carry an id and a byte count
                        for a stream the dump walks; a word only the row's own
                        fetch and count functions touch belongs to that row.
  the log line          a store in the same straight line as a resolved wlog
                        call is that line's own state, so the line's module tag
                        owns the word and its words name the role when the line
                        says what the role is.
  the accessor set      a word every function that reaches it belongs to one
                        module belongs to that module, and what the code does
                        with the value is its shape. Which module a function
                        belongs to is the log-tag partition plus what the map's
                        own derivations established, under the one spelling
                        abi/module_aliases.py settles.
  the struct's owner    a word inside the extent a base word's field accesses
                        establish is a field of that struct, and the struct
                        belongs to whoever initialises it, not to everyone who
                        reads it.

A word several modules touch and no struct places stays unnamed: which of them
owns it is not this code's to decide, and the module set is reported instead.
Nothing here guesses a struct layout either -- where the uses show field
offsets from a word the extent goes in `size` and the fields stay in the
headers, where C lives.
"""

import argparse
import bisect
import collections
import json
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autonames  # noqa: E402  (the image, the disassembly and the partition)
import callargs  # noqa: E402
import module_aliases  # noqa: E402  (the tags that are one module, from facts.yaml)
import symbols as symmap  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
CLASS = "global"

RAM_LO, RAM_HI = 0x20000000, 0x20040000

# The dblib API, from abi/include/withings/dblib.h's function comments: the
# writers take (id, buf, len) and the readers (id, buf, len) or (cursor, id,
# len). Only the writers name a cache word, because only a writer stores the
# word it persists.
DBLIB_WRITERS = (0x47DF0, 0x47E58, 0x47EBC, 0x47F58, 0x480F0, 0x4814C, 0x48200)

# The debug dump's RAM item table, declared as debug_dump_ram_items[6] in
# dblib.h; the bounds are the pair debug_dump_dblib_ram__2 loads.
DUMP_ITEMS, DUMP_ROWS, DUMP_STRIDE = 0x27CEC, 6, 0xC

# `[M]` is main.c's tag and `[INIT]` is every module's start-up line, so
# neither says whose word this is.
GENERIC_TAGS = frozenset(("M", "INIT"))

# What a log line has to say for its words to be a role and not a milestone.
# "[BAT] Charge enable" names the word the store beside it writes; "[BAT] ---"
# names the moment and nothing in memory.
ROLE_WORDS = frozenset((
    "enable", "enabled", "disable", "disabled", "start", "started", "stop",
    "stopped", "state", "status", "count", "counter", "timeout", "mode",
    "flag", "level", "index", "offset", "period", "interval", "threshold"))

LOAD_STORE = {"ldr": 4, "ldr.w": 4, "str": 4, "str.w": 4, "ldrb": 1,
              "ldrb.w": 1, "strb": 1, "strb.w": 1, "ldrh": 2, "ldrh.w": 2,
              "strh": 2, "strh.w": 2, "ldrsb": 1, "ldrsb.w": 1, "ldrsh": 2,
              "ldrsh.w": 2}
# A call may leave anything in the caller-saved registers, so an address still
# sitting in one afterwards is not the address the block loaded.
CLOBBERED = ("r0", "r1", "r2", "r3", "r12", "lr")

IE_DECL = re.compile(r"^\s*DBLIB_IE_(\w+)\s*=\s*(0x[0-9a-fA-F]+|\d+)\s*,", re.M)
PLACEHOLDER = re.compile(r"^[0-9A-F]{3}$")


def dblib_ids(path=None):
    """{id: name} from the enum in abi/include/withings/dblib.h.

    The header is the repo's one list of what a dblib id is; the members whose
    name is the id in hex are the ids nothing has named yet, and those come
    back as None so a caller can tell "unnamed" from "named DBLIB_IE_004".
    """
    path = path or os.path.join(HERE, "include", "withings", "dblib.h")
    with open(path) as fh:
        text = fh.read()
    out = {}
    for name, value in IE_DECL.findall(text):
        out[int(value, 0)] = None if PLACEHOLDER.match(name) else name.lower()
    if not out:
        sys.exit("abi/globals.py: %s declares no dblib_ie members" % path)
    return out


class Ram(object):
    """Every static RAM address the application's own instructions reach.

    A forward reading of each function the export bounds, with a register file
    that holds only the RAM addresses the function's literal pool gave it: the
    `ldr`/`str` whose base is one of those is a static access, and its
    displacement and width are the word it touches. This is the same block
    reading abi/callargs.py does for a call, run over a whole function, because
    a cache word is loaded once at the top and used throughout.
    """

    def __init__(self, img, ex):
        self.by_function = {}
        self.loaders = collections.defaultdict(set)
        self.storers = collections.defaultdict(set)
        self.stores = collections.defaultdict(list)
        self.width = collections.defaultdict(set)
        self.stored = collections.defaultdict(set)
        starts = [a for a, _, _ in img.insns]
        for fn in ex.fns:
            rows = self._function(img, starts, fn, fn + ex.size(fn))
            if not rows:
                continue
            self.by_function[fn] = rows
            for site, at, width, mode, value in rows:
                self.width[at].add(width)
                if mode == "load":
                    self.loaders[at].add(fn)
                else:
                    self.storers[at].add(fn)
                    self.stores[at].append((site, fn))
                    self.stored[at].add(value)

    @staticmethod
    def _function(img, starts, lo, hi):
        regs, imms, rows = {}, {}, []
        i = bisect.bisect_left(starts, lo)
        while i < len(img.insns) and img.insns[i][0] < hi:
            site, mnem, ops = img.insns[i]
            i += 1
            if mnem in ("bl", "bl.w", "blx"):
                for reg in CLOBBERED:
                    regs.pop(reg, None)
                    imms.pop(reg, None)
                continue
            width = LOAD_STORE.get(mnem)
            field = callargs.FIELD.match(ops) if width else None
            if field and field.group(2) in regs:
                at = regs[field.group(2)] + (callargs._int(field.group(3))
                                             if field.group(3) else 0)
                store = not mnem.startswith("ldr")
                rows.append((site, at, width, "store" if store else "load",
                             imms.get(field.group(1)) if store else None))
            dest = callargs.DEST.match(ops)
            if not dest or not dest.group(1).startswith("r"):
                continue
            reg = dest.group(1)
            regs.pop(reg, None)
            imms.pop(reg, None)
            if mnem.startswith("ldr") and "[pc" in ops:
                pool = callargs.POOL.search(ops)
                word = img.word(int(pool.group(1), 16) + img.base) if pool else None
                if word is None:
                    continue
                if RAM_LO <= word < RAM_HI:
                    regs[reg] = word
                else:
                    imms[reg] = word
                continue
            if mnem in ("mov", "mov.w", "movs", "movw"):
                move = callargs.MOVE.match(ops)
                if move:
                    if move.group(2) in regs:
                        regs[reg] = regs[move.group(2)]
                    if move.group(2) in imms:
                        imms[reg] = imms[move.group(2)]
                    continue
                imm = callargs.IMM.match(ops)
                if imm:
                    imms[reg] = callargs._int(imm.group(2)) & 0xFFFFFFFF
                continue
            if mnem in ("add", "add.w", "adds"):
                three = callargs.ADD3.match(ops)
                if three and three.group(2) in regs:
                    regs[reg] = regs[three.group(2)] + callargs._int(three.group(3))
        return rows

    def touched(self):
        return set(self.loaders) | set(self.storers)

    def functions(self, at):
        return self.loaders.get(at, set()) | self.storers.get(at, set())


class Extents(object):
    """The field offsets and the stride the code reads a RAM base with.

    abi/ghidra/word_uses.py records, per literal-pool word, the constant
    displacement at which the loaded address is dereferenced and the constant
    an index is scaled by before being added to it. Two pool words holding the
    same RAM address are the same object, so the union over them is how far
    into the object the image reaches, which is the object's extent. The
    layout inside that extent is C and belongs in the headers.
    """

    FIELD = re.compile(r"^field([+-]\d+):(\d+)$")
    STRIDE = re.compile(r"^stride:(\d+)$")

    def __init__(self, export, img):
        with open(os.path.join(export, "word_uses.json")) as fh:
            doc = json.load(fh)
        self.extent = {}
        self.stride = {}
        self.pointer = collections.defaultdict(bool)
        for row in doc["uses"]:
            value = img.word(row["addr"])
            if value is None or not RAM_LO <= value < RAM_HI:
                continue
            for use in row["uses"]:
                field = self.FIELD.match(use)
                if field:
                    end = int(field.group(1)) + int(field.group(2))
                    if end > 0:
                        self.extent[value] = max(self.extent.get(value, 0), end)
                    continue
                stride = self.STRIDE.match(use)
                if stride:
                    self.stride[value] = max(self.stride.get(value, 0),
                                             int(stride.group(1)))
        # The cell summaries answer the other question: what the code does with
        # the value the word holds. A value used as a load or store base is a
        # pointer, whatever the word's own width says.
        for cell in doc["cells"]:
            if RAM_LO <= cell["addr"] < RAM_HI:
                self.pointer[cell["addr"]] = any(
                    use in ("load_base", "store_base") for use in cell["uses"])


class Modules(object):
    """Which module a function belongs to, from the partition and the map.

    The partition abi/out/ghidra/modules.json places the 1821 functions a log
    tag reaches, directly or over the call graph. The map places more: a
    `wuiview`, `store`, `sensor`, `vasistas` or hand entry carries the module
    its own derivation established, and a `bymodule` attribution carries one
    with no name at all. Reading only the partition left a third of the words
    touched by nothing the partition places, which is not the same as touched
    by nothing.

    Two things bound what the map may add. A module label the partition does
    not use is a name the repo chose for a group of functions rather than a
    log tag -- `WPP_OBJECTS` beside the partition's `WPP`, `FLASH` beside
    `MCUFLASH` -- and admitting it would split one module in two and refuse
    every word on the seam, so only labels the partition itself uses are read.
    And a `bymodule` attribution read off the globals this file named is this
    file's own last output coming back as evidence, so only the attributions
    read off the call graph count.
    """

    # Classes whose module is derived from what this file wrote: a
    # provenance/role/wrapper name is `<global>_<verb>` off a global named
    # here, and a `bymodule` attribution may be `reaches only <those
    # globals>`. Taking any of them as evidence of a word's module would make
    # each run derive one more name from the last instead of settling.
    DERIVED_FROM_GLOBALS = frozenset(("role", "wrapper", "provenance",
                                      "helper", "shared", "bymodule"))

    def __init__(self, export, smap):
        with open(os.path.join(export, "modules.json")) as fh:
            doc = json.load(fh)
        self.of_function = {int(a, 16): v["module"]
                            for a, v in doc["functions"].items()}
        labels = set(doc["modules"])
        aliases = module_aliases.load()
        for s in smap.symbols:
            if s.kind not in symmap.CODE_KINDS or not s.module:
                continue
            at = s.address & ~1
            if at in self.of_function:
                continue
            if s.klass in self.DERIVED_FROM_GLOBALS and not (
                    s.klass == "bymodule"
                    and autonames.BYMODULE_BY_CALLEES in (s.evidence or "")):
                continue
            tag = s.module.upper()
            tag = aliases.get(tag, tag)
            if tag in labels:
                self.of_function[at] = tag

    def named(self, fn):
        tag = self.of_function.get(fn)
        return None if tag is None or tag in GENERIC_TAGS else tag

    def of_word(self, ram, at):
        """The one module every function touching the word logs under, or None."""
        tags = set()
        for fn in ram.functions(at):
            tag = self.named(fn)
            if tag:
                tags.add(tag)
        return tags


def shape_of(ram, extents, at):
    """What the code does with the word, as one name and a byte count.

    The reading is ordered by how much it claims: a stride is a record array, an
    extent past one word is a struct, a value dereferenced is a pointer, a word
    only ever written 0 or 1 is a flag, and anything else is the plain word the
    access width makes it.
    """
    stride = extents.stride.get(at)
    extent = extents.extent.get(at)
    if stride:
        return "table", extent or stride
    if extent and extent > 4:
        return "struct", extent
    if extents.pointer.get(at):
        return "ptr", 4
    width = max(ram.width.get(at, {1}))
    values = ram.stored.get(at, set())
    if values and None not in values and values <= {0, 1}:
        return "flag", width
    return {1: "byte", 2: "half", 4: "word"}.get(width, "word"), width


def slug(text):
    """A C identifier from a string the firmware chose, and nothing more."""
    out = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    return out if re.match(r"^[a-z_][a-z0-9_]*$", out or "") else None


# ------------------------------------------------- the settings cache words

class Setting(object):
    """One dblib-backed setting: the id, the cache word and its accessors."""

    def __init__(self, ie, name, cache, size, writers, reader):
        self.ie = ie
        self.name = name
        self.cache = cache
        self.size = size
        self.writers = writers
        self.reader = reader


def persist_calls(img, ex):
    """Every dblib write whose id the calling sequence establishes."""
    calls = []
    for i, (site, mnem, ops) in enumerate(img.insns):
        if mnem not in ("bl", "bl.w"):
            continue
        target = re.search(r"0x([0-9a-f]+)", ops.split("@")[0])
        if not target or int(target.group(1), 16) not in DBLIB_WRITERS:
            continue
        frame = callargs.resolve(img, img.insns, i)
        ie, length = frame.arg(0), frame.arg(2)
        if not isinstance(ie, (callargs.Imm, callargs.Pool)):
            continue
        calls.append((site, ex.owner(site), int(target.group(1), 16), ie.value,
                      length.value if isinstance(length, callargs.Imm) else None))
    return calls


def cache_reader(ram, ex, cache, writers):
    """The accessor whose whole body is the cache word copied out, if it is one.

    A getter is the smallest function in the image: a pool load, one load of
    the cache word and a `bx lr`. Requiring that the cache word is the only
    aligned RAM word it touches is what separates it from the module code that
    also reads the setting, and requiring it to be the only such function is
    what stops a setting with three bit-test readers from claiming one of them.
    """
    found = []
    for fn in ram.loaders.get(cache, ()):
        if fn in writers or ex.size(fn) > 24:
            continue
        reached = set(at for _, at, _, _, _ in ram.by_function[fn])
        if reached - {cache} or ram.storers.get(cache, set()) & {fn}:
            continue
        found.append(fn)
    return found[0] if len(found) == 1 else None


def settings(img, ex, ram, ids):
    """The cache word behind each dblib id, where one writer establishes it."""
    by_id = collections.defaultdict(list)
    for site, fn, api, ie, length in persist_calls(img, ex):
        if fn is not None:
            by_id[ie].append((site, fn, api, length))
    found, refused = [], []
    for ie, calls in sorted(by_id.items()):
        writers = set(fn for _, fn, _, _ in calls)
        candidates = collections.Counter()
        for _, fn, _, _ in calls:
            rows = ram.by_function.get(fn, ())
            loads = set(at for _, at, _, mode, _ in rows if mode == "load")
            stores = set(at for _, at, _, mode, _ in rows if mode == "store")
            candidates.update(loads & stores)
        if not candidates:
            refused.append((ie, "no writer both reads and writes a static word:"
                                " the setting is written from a caller's buffer"))
            continue
        if len(candidates) > 1:
            refused.append((ie, "the writers touch %d static words (%s), so the"
                                " setting is a struct and which word is which"
                                " is not established"
                            % (len(candidates),
                               ", ".join("0x%x" % a for a in sorted(candidates)))))
            continue
        cache = list(candidates)[0]
        sizes = set(length for _, _, _, length in calls if length is not None)
        name = ids.get(ie)
        found.append(Setting(ie, name or "dblib_setting_0x%x" % ie, cache,
                             sizes.pop() if len(sizes) == 1 else None,
                             sorted(writers), cache_reader(ram, ex, cache, writers)))
    return found, refused


def setting_rows(found, ram, extents, modules, taken):
    """The cache word and the accessors of each setting, with their evidence.

    `taken` is every name the map already carries: the setting's own name is
    the word's name, except where the map spends it on something else, and then
    the word is the cache of that setting rather than the setting itself.
    """
    rows = []
    for s in found:
        shape, size = shape_of(ram, extents, s.cache)
        word = s.name if s.name not in taken else "%s_cache" % s.name
        carried = ("dblib id 0x%x, persisted by %s with"
                   % (s.ie, ", ".join("0x%x" % w for w in s.writers)))
        tags = modules.of_word(ram, s.cache)
        rows.append(dict(address=s.cache, name=word, kind="global",
                         module=tags.pop() if len(tags) == 1 else None,
                         shape=shape, size=s.size or size,
                         evidence="the cache word of %s: %s dblib_set(0x%x,"
                                  " &0x%x, %s)"
                                  % (s.name, carried, s.ie, s.cache,
                                     s.size if s.size else "a length the block"
                                                           " does not establish")))
        if len(s.writers) == 1:
            rows.append(dict(address=s.writers[0], name="%s_set" % s.name,
                             kind="function", shape="accessor",
                             evidence="the only function that persists dblib id"
                                      " 0x%x, comparing and storing the cache"
                                      " word at 0x%x first" % (s.ie, s.cache)))
        if s.reader is not None:
            rows.append(dict(address=s.reader, name="%s_get" % s.name,
                             kind="function", shape="accessor",
                             evidence="its whole body copies the cache word at"
                                      " 0x%x out; dblib id 0x%x"
                                      % (s.cache, s.ie)))
    return rows


# --------------------------------------------- the debug dump's RAM items

def dump_rows(img, ram, extents, ids):
    """The words only one RAM item's own fetch and count functions touch.

    The table's rows are (id, byte count, fetch, count): the record is what the
    fetch writes into the caller's buffer, so a row names a RAM address only
    where its own two functions are the only ones that reach one.
    """
    rows, refused = [], []
    for i in range(DUMP_ROWS):
        at = DUMP_ITEMS + i * DUMP_STRIDE
        offset = at - img.base
        ie, size = struct.unpack_from("<HH", img.data, offset)
        fetch, count = struct.unpack_from("<II", img.data, offset + 4)
        owners = {fetch & ~1, count & ~1}
        name = ids.get(ie) or "dblib_ram_item_0x%x" % ie
        mine = sorted(a for a in ram.touched()
                      if ram.functions(a) and ram.functions(a) <= owners)
        if not mine:
            refused.append((ie, "the row's fetch 0x%x and count 0x%x reach no"
                                " static word of their own: the record is built"
                                " in the caller's buffer"
                            % (fetch & ~1, count & ~1)))
            continue
        for word in mine:
            shape, width = shape_of(ram, extents, word)
            rows.append(dict(address=word,
                             name="%s_%s_%x" % (name, shape, word),
                             kind="global", module="DEBUG_DUMP", shape=shape,
                             size=width,
                             evidence="only debug_dump_ram_items row %d"
                                      " (id 0x%x, %d bytes) reaches it, through"
                                      " its fetch 0x%x and count 0x%x"
                                      % (i, ie, size, fetch & ~1, count & ~1)))
    return rows, refused


# ------------------------------------------------------ the log-line rules

def logline_rows(img, ex, ram, extents, sites, taken):
    """Words a store writes in the same straight line as a resolved log line.

    The block abi/callargs.py reads for a call is the run of instructions
    ending at it and beginning after the last control transfer, which is the
    largest window in which one instruction's effect and another's are the same
    step. A store inside the window of a wlog call is that line's own state.
    """
    by_site = {}
    for site in sites:
        if site["fmt"] and autonames.log_tag(site["fmt"]):
            by_site[site["site"]] = site
    index = {addr: i for i, (addr, _, _) in enumerate(img.insns)}
    lines = collections.defaultdict(set)
    for at, stores in ram.stores.items():
        for store, _ in stores:
            i = index[store]
            for call in sorted(by_site):
                if not store < call or call - store > callargs.MAX_BLOCK:
                    continue
                j = index[call]
                if callargs.block_start(img.insns, j) <= i:
                    lines[at].add(call)
                break
    rows, refused = [], []
    # A role the same line gives two words names neither of them: the line is
    # about the step, and which of the two words the step's name belongs to is
    # not something the line says.
    roles = collections.Counter()
    for at, calls in lines.items():
        if len(calls) == 1 and len(ram.storers[at]) == 1:
            roles[autonames.logline_slug(by_site[list(calls)[0]]["fmt"])] += 1
    for at, calls in sorted(lines.items()):
        tags = set(autonames.log_tag(by_site[c]["fmt"]) for c in calls)
        if len(tags) > 1:
            refused.append((at, "stored beside lines of %s"
                            % ", ".join(sorted(tags))))
            continue
        tag = tags.pop()
        if tag in GENERIC_TAGS:
            refused.append((at, "the only line beside it is tagged [%s], which"
                                " is the boot's own tag and no module's" % tag))
            continue
        shape, size = shape_of(ram, extents, at)
        name, why = None, None
        if len(calls) == 1 and len(ram.storers[at]) == 1:
            fmt = by_site[list(calls)[0]]["fmt"]
            role = autonames.logline_slug(fmt)
            if (role and roles[role] == 1 and role not in taken
                    and ROLE_WORDS & set(role.split("_"))):
                name, why = role, "the line names the role"
        rows.append(dict(address=at, name=name or "%s_%s_%x"
                         % (tag.lower(), shape, at),
                         kind="global", module=tag, shape=shape, size=size,
                         evidence="stored in the same straight line as %s%s"
                                  % (", ".join("wlog(%r) at 0x%x"
                                               % (by_site[c]["fmt"], c)
                                               for c in sorted(calls)),
                                     "; " + why if why else "")))
    return rows, refused


# ------------------------------------------------ the accessor-set rule

def struct_fields(ram, extents, modules):
    """For each word inside a struct extent, the module of the base's writer.

    A word several modules touch is usually not several modules' word: it is
    one field of one context struct that a producer fills and a consumer
    reads, and the reader's module says nothing about whose struct it is. The
    base's writer does. Where a word sits inside the extent some pool word's
    field accesses establish and the functions that store the base word all
    belong to one module, the struct is that module's and so is the field,
    whoever else reads it.

    Bases whose writers span two modules answer nothing and are left out, and
    so is a word covered by two structs whose writers disagree: then which
    struct the word is a field of is exactly what is not established.
    """
    owner = {}
    for base, extent in extents.extent.items():
        if extent <= 4 or extents.stride.get(base):
            continue
        tags = set()
        for fn in ram.storers.get(base, ()):
            tag = modules.named(fn)
            if tag:
                tags.add(tag)
        if len(tags) != 1:
            continue
        tag = tags.pop()
        for at in range(base + 1, base + extent):
            if at in owner and owner[at][0] != tag:
                owner[at] = (None, None)
                continue
            owner[at] = (tag, base)
    return dict((at, v) for at, v in owner.items() if v[0])


def module_rows(ram, modules, extents):
    """Words one module owns: every function that touches them is in it, or
    they are a field of a struct one module's code initialises."""
    fields = struct_fields(ram, extents, modules)
    rows, refused = [], []
    for at in sorted(ram.touched()):
        tags = modules.of_word(ram, at)
        if len(tags) == 1:
            tag, why = tags.pop(), None
        elif at in fields:
            tag, base = fields[at]
            why = ("it is +0x%x of the struct at 0x%x, which only [%s] writes"
                   " the base word of%s"
                   % (at - base, base, tag,
                      "; %s also reach it" % "/".join(sorted(tags)) if tags
                      else " and no function reaching it is in a module"))
        else:
            refused.append((at, sorted(tags)))
            continue
        shape, size = shape_of(ram, extents, at)
        rows.append(dict(address=at, name="%s_%s_%x" % (tag.lower(), shape, at),
                         kind="global", module=tag, shape=shape, size=size,
                         evidence=why or
                                  "every function that loads or stores it logs"
                                  " under [%s] (%d read it, %d write it)"
                                  % (tag, len(ram.loaders.get(at, ())),
                                     len(ram.storers.get(at, ())))))
    return rows, refused


# ------------------------------------------------------------------ writing

def merge(*groups):
    """One address, one name: the strongest reading that reached it keeps it.

    The groups are given strongest first, so a settings cache word is not also
    named by its module and a name the settings rule took is not taken twice.
    """
    rows, by_address, by_name = [], {}, {}
    for group in groups:
        for row in group:
            if row["address"] in by_address or row["name"] in by_name:
                continue
            by_address[row["address"]] = row
            by_name[row["name"]] = row
            rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default=os.path.join(SIM, "appl.bin"))
    ap.add_argument("--dis", default=os.path.join(SIM, "out", "appl.dis"))
    ap.add_argument("--export", default=os.path.join(HERE, "out", "ghidra"))
    ap.add_argument("--report", action="store_true",
                    help="print what each word is named from; write nothing")
    args = ap.parse_args()

    img = autonames.Image(args.image, args.dis)
    ex = autonames.Export(args.export)
    if not ex.ok:
        sys.exit("abi/globals.py: no export under %s" % args.export)
    ram = Ram(img, ex)
    extents = Extents(args.export, img)
    prior = symmap.load()
    modules = Modules(args.export, prior)
    ids = dblib_ids()

    found, no_cache = settings(img, ex, ram, ids)
    # Not this run's own last output: a name keyed on what the map holds after
    # the previous run would see itself as the image's naming and back away
    # from it, and the second run would differ from the first.
    taken = set(name for name, s in prior.by_name.items() if s.klass != CLASS)
    cached = setting_rows(found, ram, extents, modules, taken)
    dumped, no_item = dump_rows(img, ram, extents, ids)
    logged, mixed_tags = logline_rows(img, ex, ram, extents,
                                      autonames.wlog_sites(img, ex),
                                      taken)
    owned, shared = module_rows(ram, modules, extents)
    rows = merge(cached, dumped, logged, owned)

    # A word the map already names from a create, a header or a store is
    # settled by a stronger reading than any of these, so it keeps its name and
    # the disagreement is reported rather than resolved here. Where the map's
    # reading is the weaker one -- a nickname off the call graph, or the line a
    # setter happens to log -- the dblib id is what the address is about, so
    # the rewrite displaces it, which is what the class ranking is for.
    kept, held = [], []
    for row in rows:
        at = prior.by_address.get(row["address"])
        settled = at is not None and at.klass in symmap.SETTLED
        if at is not None and at.klass != CLASS and (
                settled or not symmap.outranks(CLASS, at.klass)):
            held.append((row, at))
            continue
        row["class"] = CLASS
        row.pop("shape", None)
        if row.get("module") is None:
            row.pop("module", None)
        kept.append(row)

    print("%d settings with a cache word, %d ids with none;"
          " %d words from the debug dump, %d from a log line, %d from a module"
          % (len(found), len(no_cache), len(dumped), len(logged), len(owned)))
    print("%d names, %d dropped to a stronger reading in the map"
          % (len(kept), len(held)))
    if args.report:
        for s in sorted(found, key=lambda s: s.ie):
            print("  0x%-5x %-34s cache 0x%08x  %-6s get %s  set %s"
                  % (s.ie, s.name, s.cache,
                     ("%d b" % s.size) if s.size else "?",
                     "0x%x" % s.reader if s.reader else "-",
                     ", ".join("0x%x" % w for w in s.writers)))
        for ie, why in no_cache:
            print("  id 0x%-5x refused: %s" % (ie, why))
        for ie, why in no_item:
            print("  dump item 0x%-4x refused: %s" % (ie, why))
        for at, why in mixed_tags:
            print("  0x%x refused: %s" % (at, why))
        for row, at in held:
            print("  0x%x is %s (%s) in the map and %s here"
                  % (row["address"], at.name, at.klass, row["name"]))
        several = [(a, t) for a, t in shared if t]
        print("  %d words several modules touch, %d words only functions in no"
              " module touch: %s"
              % (len(several), len(shared) - len(several),
                 ", ".join("0x%x %s" % (a, "/".join(t))
                           for a, t in several[:20])))
        return 0

    try:
        # The settings rule settles its addresses rather than reading them: the
        # id is an argument at the persist call, the way a FreeRTOS create
        # carries the name of the object it is handed.
        added = symmap.load().rewrite(
            kept, {CLASS}, verified=set(row["address"] for row in cached))
    except symmap.Refusal as err:
        sys.exit("abi/globals.py: abi/symbols.yaml: %s" % err)
    print("wrote %d entries to abi/symbols.yaml" % added)
    return 0


if __name__ == "__main__":
    sys.exit(main())
