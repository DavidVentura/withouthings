#!/usr/bin/env python3
"""Decide, for every candidate word in the app, whether it holds an address.

    python3 abi/classify_words.py [--export abi/out/ghidra]

Source code contains no addresses, so every absolute address in the image sits
in a slot the compiler reserved for a linker relocation: a literal-pool word, a
word of an absolute jump table, or a word inside a data item. The candidates are
therefore every 32-bit word of the image that is not an instruction and whose
value lands in the app's flash range or the app's RAM range, which the partition
export already enumerates (references.json's `words`).

What is left is the classification, because a constant can look like an address:
0x493e0 is five minutes in milliseconds and also an address inside this image.
The signals, strongest first, are below in DECISIONS; every word records the one
that decided it, and what none of them decides goes on the review list rather
than being guessed at.

What the per-word shape cannot decide is taken up by abi/runs.py, which decides
the untyped runs from their own references: a run nothing can reach, a run some
address inside it names at a byte boundary, and the record grid the aligned
references mark out. Its signals are recorded like any other.

abi/words.yaml holds the hand-declared facts: words whose shape says nothing or
says the wrong thing, each with the argument for what it is. They are applied
first and the heuristics never overrule one.

One shape the plan did not list turns up in the flow: `ldr rN,[pc,#k]` followed
by `add rN,pc`, 29 words in one compilation unit, where the word holds the
distance from the adding instruction to its target rather than the target. Such
a word is neither a pointer nor a number: it is stale as soon as either end
moves alone, and 28 of the 29 name RAM. They are written out as
`displacements`, and blobify emits each RAM one as an R_ARM_REL32 so the
linker recomputes it; the one naming flash is pinned at both ends instead.

The use signal comes from abi/ghidra/word_uses.py, which follows every value a
pc-relative load defines forward through the function's p-code and through the
callees it is handed to. Only its pointer half is applied: a value that is
branched to or dereferenced is an address, and measured against the structural
signals it never contradicts one. Its constant half is not applied, because in
this image 94 words the structure proves to be string and function pointers are
only ever compared -- a table sentinel test and an integer bound test are the
same instruction -- so "only compared" is not evidence of a number.

Writes abi/out/ghidra/words.json: one row per candidate with its class, its
target and addend, the deciding signal, and a summary with the counts per
bucket. abi/objectify.py turns the `pointer` rows into R_ARM_ABS32.
"""

import argparse
import collections
import json
import os
import re
import sys

import yaml

import peripherals
import runs
import shapes
import symbols

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)

APP_BASE = 0x27000
APP_END = 0xF117C
RAM_BASE, RAM_END = 0x20000000, 0x20040000

def read_facts(path, blob, functions):
    """abi/words.yaml: the contracts by value and the overrides by address.

    Every override is checked against the image here, so an entry that the
    image no longer agrees with is refused rather than applied to whatever the
    word now holds, and a pointer's target is resolved to an address once.
    """
    spec = yaml.safe_load(open(path))
    reasons = spec["reasons"]
    contracts = {int(v): " ".join(str(why).split())
                 for v, why in spec["contracts"].items()}
    overrides = {}
    for entry in spec["overrides"]:
        addr = entry["address"]
        held = int.from_bytes(blob[addr - APP_BASE:addr - APP_BASE + 4], "little")
        if held != entry["value"]:
            sys.exit("abi/words.yaml claims 0x%x holds 0x%08x; the image holds"
                     " 0x%08x" % (addr, entry["value"], held))
        if entry["why"] not in reasons:
            sys.exit("abi/words.yaml gives 0x%x the reason %s, which it does not"
                     " state" % (addr, entry["why"]))
        # A reason named after a run is a measurement, not an argument, so the
        # run and the site that produced it travel with the entry and
        # abi/observe_words.py --apply is what writes them.
        observed = entry.get("observed")
        if entry["why"].startswith("observed_"):
            if not observed or not all(k in observed for k in
                                       ("signal", "run", "evidence")):
                sys.exit("abi/words.yaml gives 0x%x the observed reason %s"
                         " without the signal, the run and the evidence"
                         % (addr, entry["why"]))
        elif observed is not None:
            sys.exit("abi/words.yaml gives 0x%x an observation under the reason"
                     " %s, which is not one a run decided"
                     % (addr, entry["why"]))
        if entry["class"] == "pointer":
            target = entry["target"]
            if not isinstance(target, int):
                named = [f["start"] for f in functions if f["name"] == target]
                if len(named) != 1:
                    sys.exit("abi/words.yaml points 0x%x at %s, which the"
                             " partition names %d times" % (addr, target, len(named)))
                target = named[0]
            entry = dict(entry, target=target + entry["addend"])
        elif entry["class"] != "constant":
            sys.exit("abi/words.yaml gives 0x%x the class %s, which is neither"
                     " constant nor pointer" % (addr, entry["class"]))
        if addr in overrides:
            sys.exit("abi/words.yaml declares 0x%x twice" % addr)
        overrides[addr] = entry
    return contracts, overrides


def word_shaped(start, end, blob):
    """Is this object an array of 32-bit words, so that a word in it is a slot?

    A 4-aligned window over a table of 6-byte records reads two halves of two
    fields as one address often enough to matter: this image's unit table at
    0xbe4d0 gave four of them, and relocating those corrupted the table. The
    test is classify_gaps.py's pointer-table test applied to the object rather
    than to the run: every word has to be zero or an address, which no record
    table of mixed integers satisfies.
    """
    if start % 4 or (end - start) % 4 or end - start < 8:
        return False
    for at in range(start, end, 4):
        v = int.from_bytes(blob[at - APP_BASE:at - APP_BASE + 4], "little")
        if v and not (APP_BASE <= (v & ~1) < APP_END or RAM_BASE <= v < RAM_END):
            return False
    return True


PRINTABLE_MIN = 3


class Partition(object):
    """Where an address lands: the starts the export knows and the spans."""

    def __init__(self, items, words, blob):
        self.functions = {f["start"]: f for f in items["functions"]}
        self.starts = {}
        for d in items["data"]:
            self.starts[d["start"]] = d.get("name") or "data"
        for r in items["array_rows"]:
            self.starts.setdefault(r["start"], "array_row")
        self.code = sorted((r["start"], r["end"], r["function"])
                           for r in items["function_ranges"])
        # Literal pools and jump tables are deliberately not objects here. A
        # pool word is a slot private to one function, reached only by that
        # function's pc-relative loads, so nothing in the image names it and a
        # value that happens to equal one is a coincidence. Ten words of a
        # u16-pair table read as pointers to `contrast_task_create`'s pool
        # because 0x000305c8 is {0x05c8, 0x0003}, and relocating them corrupted
        # the table as soon as that function moved.
        self.items = sorted((d["start"], d["end"]) for d in items["data"])
        self.gaps = sorted((g["start"], g["end"]) for g in items["gaps"])
        self.blob = blob
        bits = bytes.fromhex(items["instruction_bytes"]["bits"])
        self.covered = bytes((bits[i >> 3] >> (i & 7)) & 1
                             for i in range(len(blob)))
        self.slots = None
        self.copy_sources = None
        # The chip's register map, and how many instructions read each word:
        # a peripheral address is one the code loads, so a data word holding
        # one names nothing.
        self.chip = None
        self.read_sites = {}
        # The recovered copies as ranges, and the memory-cell use summaries
        # abi/ghidra/word_uses.py writes; together they answer what the code
        # does with a word that only exists in RAM.
        self.copies = ()
        self.cells = {}

        # A word inside a function body that an instruction reads is data the
        # compiler put between two basic blocks; something pointing at it is a
        # pointer, not a stray constant that happens to land in code.
        # Every candidate inside a function body is data the compiler left
        # between two basic blocks, whether or not a literal load reads it;
        # something pointing at one is a pointer, not a stray constant.
        self.in_body_words = set(w["addr"] for w in words if self.in_code(w["addr"]))

    def decides(self, word, overrides):
        """Whether the copy shape is what decides `word`, and so is evidence.

        The shape's docstring argues only for the case where nothing else
        speaks: a word another rule decides is named by something else, and the
        two RAM addresses its function happens to hold measure out a length
        nothing says is this word's -- 0x50e8c and 0x50e94 both come out at
        78904 bytes, which they cannot both be. The rules that run before it are
        the hand-written overrides and the forward-flow pointer use, so a copy
        is evidence exactly where neither fires.
        """
        return (word["addr"] in self.copy_sources
                and word["addr"] not in overrides
                and not set(word.get("uses") or ()) & POINTER_USES)

    def string_run(self, value):
        """(start, end) of the printable NUL-terminated run `value` is in.

        The run is bounded by the NUL in front of it and the NUL that ends it,
        neither of which the disassembly may cover, and its bytes have to be
        text. The UI text is UTF-8 (accented French, the degree sign, a
        non-breaking space in one log line), so the run is decoded rather than
        tested byte by byte; a run that is not well-formed UTF-8 or holds a
        control character is not text.
        """
        at = value - APP_BASE
        if not 0 < at < len(self.blob) or self.covered[at] or not self.blob[at]:
            return None
        end = at
        while end < len(self.blob) and self.blob[end]:
            if self.covered[end]:
                return None
            end += 1
        if end >= len(self.blob):
            return None
        start = at
        while start and self.blob[start - 1]:
            if self.covered[start - 1]:
                return None
            start -= 1
        if not start:
            return None
        try:
            text = self.blob[start:end].decode("utf-8")
        except UnicodeDecodeError:
            return None
        if not all(ord(c) >= 0x20 and not 0x7F <= ord(c) <= 0x9F or c in "\t\n\r"
                   for c in text):
            return None
        return start + APP_BASE, end + APP_BASE

    def names_a_string(self, value):
        """Is `value` the first byte of a printable NUL-terminated run?

        309 words of this image are a pointer to a string the partition never
        typed, because the run sits inside an untyped gap and the analysis had
        no reference to start it from. The shape is specific enough to decide
        on: the byte before is a NUL, the run is printable ASCII to its
        terminator, and the disassembly covers none of it. Anything else that
        happened to hold the address of such a byte would have to reproduce the
        whole address, which is the same argument a function start gets.

        A value further into such a run is a different question, because an
        integer that happens to land inside the string region reproduces
        nothing: abi/runs.py takes those, and only with a column beside them.
        """
        run = self.string_run(value)
        return run is not None and run[0] == value and run[1] - value >= PRINTABLE_MIN

    def is_slot(self, addr):
        return addr in self.slots

    def item_of(self, value):
        """The item `value` lands in, or None: strings are indexed from the
        middle often enough that an interior hit is an ordinary pointer."""
        lo, hi = 0, len(self.items)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.items[mid][0] <= value:
                lo = mid + 1
            else:
                hi = mid
        if lo and self.items[lo - 1][1] > value:
            return self.items[lo - 1][0]
        return None

    def in_code(self, value):
        lo, hi = 0, len(self.code)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.code[mid][0] <= value:
                lo = mid + 1
            else:
                hi = mid
        return lo and self.code[lo - 1][1] > value


# The operations that can only be applied to an address. `offset`, `compare`,
# `scaled`, `stored_value` and `returned` are recorded too, and none of them is
# evidence either way: pointers are compared, stored and returned constantly.
POINTER_USES = frozenset(("call", "load_base", "store_base"))

FIELD_USE = re.compile(r"^field([+-]\d+):(\d+)$")


def selected_registers(chip, base, uses):
    """The registers a reader's own immediates select from a peripheral base.

    A driver loads the block's base once and reaches every register through an
    offset the instruction carries, so the word alone names the peripheral and
    the pair names the register. word_uses.py already records the displacement
    and the width of every dereference of the loaded value, which is that
    offset, so the registers are read off the reading code rather than guessed
    from the block's span.
    """
    found = []
    for use in uses:
        matched = FIELD_USE.match(use)
        if not matched:
            continue
        at = chip.register_after(base, int(matched.group(1)))
        if at is not None and at.register is not None:
            found.append(at.register.name)
    return sorted(set(found))


def peripheral_note(chip, word, uses):
    """What `word` names in the chip's register map, or None.

    Two conditions beyond the value landing on a register. The word has to be
    one the code loads, because a peripheral address is reached through a
    literal pool and a data word that happens to hold 0x40000000 reproduces
    nothing; and where the flow summary says what the loaded value was used
    for, it has to have been dereferenced. A word the code only ever compares
    is a number that collided with the range -- 0x73a20 holds two of them and
    compares them two thousand times -- which is the one reading a register
    address has no use for.
    """
    located = chip.at(word["value"])
    if located is None:
        return None
    if uses and not uses & POINTER_USES:
        return None
    if located.peripheral.base != word["value"]:
        return located.name
    # The value is the block's own base, which is how a driver holds a
    # peripheral: the register is whatever offset the reading instruction
    # carries, and a register that happens to sit at offset zero is one of
    # those and not the word's meaning.
    selected = selected_registers(chip, word["value"], uses)
    if not selected:
        return "NRF_%s" % located.peripheral.name
    return "NRF_%s, read at %s" % (located.peripheral.name, ", ".join(selected))

STRIDES = tuple(range(4, 68, 4))
MIN_RECORDS = 3


def pointer_fields(start, end, blob, plausible):
    """The positions of a record table that hold a pointer in every record.

    An untyped run is usually a table of records mixing integers and pointers,
    and a 4-aligned window over it reads two halves of two fields as often as it
    reads a field: the unit table at 0xbe4d0 has 6-byte records and gave four
    false addresses. What a real pointer field looks like is that every record
    has one at the same offset, so the stride and the offset are what has to be
    found, not the individual word. A field is only claimed when every one of at
    least three records holds either zero or something the partition can name.
    """
    slots = []
    for stride in STRIDES:
        if (end - start) < stride * MIN_RECORDS:
            break
        for offset in range(0, stride, 4):
            at = start + offset + (-(start + offset)) % 4
            positions = list(range(at, end - 3, stride))
            if len(positions) < MIN_RECORDS:
                continue
            values = [int.from_bytes(blob[p - APP_BASE:p - APP_BASE + 4], "little")
                      for p in positions]
            if all(v == 0 for v in values) or not all(plausible(v) for v in values):
                continue
            slots.extend(positions)
    return slots


def slot_objects(items, blob, part):
    """Every address at which a word may be read as a word.

    A literal pool and an absolute jump table are words by construction, so the
    export's own kinds carry them. What is left is the data items and the
    untyped runs, where a word is only a slot if the object is an array of them
    or if it is a pointer field of a record table.
    """

    def plausible(v):
        if v == 0 or RAM_BASE <= v < RAM_END:
            return True
        if v & 1 and (v & ~1) in part.functions:
            return True
        return v in part.starts

    slots = set()
    for d in items["data"]:
        if d["class"] == "string":
            continue
        # Ghidra types a word it resolved a reference from as a pointer, and a
        # word something read as a word as `undefined4`; either way the object
        # is one word and the analysis already said where it starts.
        if (d["type"].endswith("*") or d["type"] == "undefined4") \
                and d["start"] % 4 == 0 and d["end"] - d["start"] == 4:
            slots.add(d["start"])
        elif word_shaped(d["start"], d["end"], blob):
            slots.update(range(d["start"], d["end"] - 3, 4))
        else:
            slots.update(pointer_fields(d["start"], d["end"], blob, plausible))
    for g in items["gaps"]:
        if word_shaped(g["start"], g["end"], blob):
            slots.update(range(g["start"], g["end"] - 3, 4))
        else:
            slots.update(pointer_fields(g["start"], g["end"], blob, plausible))
    return slots


COPY_WINDOW = 16


def copy_initialisers(items, refs, blob, part):
    """Pool words that are the flash source of a RAM initialiser image.

    A compiler emits `memcpy(&__data_start__, &__data_load__, &__data_end__ -
    &__data_start__)` as three pool words next to each other: two RAM addresses
    whose difference is the length, and one flash address that is the image. The
    flash word is invisible to every other signal here -- the bytes it names are
    an untyped gap, the value is even, and nothing else in the image names the
    run -- so the copy is the only thing that says it is an address at all, and
    a copy the linker does not relocate reads the wrong bytes as soon as the run
    moves.

    The shape is all four of: the reading function calls memcpy or memmove; the
    word is 4-aligned and lands in the image; two more of that function's pool
    words within COPY_WINDOW bytes hold RAM addresses; and no instruction covers
    the run the difference measures out from the value. A copy loop the compiler
    wrote out by hand would qualify too, but the export carries no disassembly
    to recognise one by, and the image's copies all call memcpy.

    A function whose pool holds more than two RAM addresses offers more than one
    difference and so names no one length; that is refused rather than guessed
    at, because the length is half of what the entry claims.
    """
    names = {f["start"]: f["name"] for f in items["functions"]}
    copiers = set(a for a, n in names.items() if n in ("memcpy", "memmove"))
    callers = set(c["function"] for c in refs["calls"] if c["to"] in copiers)
    pools = collections.defaultdict(set)
    for r in refs["pool_reads"]:
        pools[r["function"]].add(r["target"])

    def held(addr):
        return int.from_bytes(blob[addr - APP_BASE:addr - APP_BASE + 4], "little")

    found = {}
    for fn in sorted(callers & set(pools)):
        near_all = sorted(pools[fn])
        for word in near_all:
            value = held(word)
            if value % 4 or not APP_BASE <= value < APP_END:
                continue
            ram = [held(q) for q in near_all if abs(q - word) <= COPY_WINDOW
                   and RAM_BASE <= held(q) < RAM_END and held(q) % 4 == 0]
            spans = set()
            for i, lo in enumerate(ram):
                for hi in ram[i + 1:]:
                    length = abs(hi - lo)
                    if not length or length % 4 or value + length > APP_END:
                        continue
                    if any(part.covered[k] for k in
                           range(value - APP_BASE, value + length - APP_BASE)):
                        continue
                    spans.add((min(lo, hi), length))
            if len(spans) == 1:
                (dest, length), = spans
                found[word] = (names[fn], dest, length)
    # A byte belongs to one object, so two candidates whose source runs overlap
    # cannot both be a copy and neither of them is evidence: the shape is two
    # RAM addresses in a memcpy caller's pool and a difference that measures out
    # an uncovered run, and a function holding a pair far apart produces a run
    # tens of kilobytes long that swallows whatever is near it. 0xd944c..0xec884
    # and 0xde23c..0xe6aec are that, and they are each other's refutation.
    order = sorted(found, key=lambda w: (held(w), found[w][2]))
    dropped = set()
    for i, w in enumerate(order):
        for other in order[i + 1:]:
            if held(other) >= held(w) + found[w][2]:
                break
            dropped.add(w)
            dropped.add(other)
    return {w: v for w, v in found.items() if w not in dropped}


def initialiser_cell(part, addr):
    """The RAM cell a flash word inside a recovered copy ends up as.

    A RAM initialiser image is copied wholesale and then read only through RAM:
    no instruction in flash names any word of it but the first, so every
    structural signal here is blind to it and every one of its words that is not
    plainly a number ends up on the review list. What the copy gives is a map --
    the word at `source + off` is the word at `destination + off` -- and
    abi/ghidra/word_uses.py's memory cells say what the code does with the value
    it loads from an absolute address. Composing the two decides the flash word
    by what its RAM image is used as.
    """
    for source, length, dest in part.copies:
        if source <= addr < source + length:
            return part.cells.get(dest + (addr - source))
    return None


def decide(word, part, contracts, overrides):
    """(class, signal, note) for one candidate word.

    class is "pointer", "constant" or "review"; signal names the rule that
    fired, which is what a later argument about a wrong decision has to attack.
    """
    value, thumb = word["value"], word["thumb"]
    target = value & ~1 if thumb else value
    uses = set(word.get("uses") or ())
    dereferenced = uses & POINTER_USES

    if word["addr"] in overrides:
        # Hand-declared, so no rule below is consulted at all: an override is
        # someone's argument about this exact word, and a heuristic that
        # disagreed with it would only be re-running the shape test the
        # argument was made to answer.
        return overrides[word["addr"]]["class"], "override", \
            overrides[word["addr"]]["why"]
    if value in contracts:
        return "constant", "contract", contracts[value]
    if word.get("pc_sites"):
        return "constant", "pc_relative", \
            "distance from %s to %s" % (
                ", ".join("0x%x" % s for s in word["pc_sites"]),
                ", ".join("0x%x" % ((value + s + 4) & 0xFFFFFFFF)
                          for s in word["pc_sites"]))
    if RAM_BASE <= value < RAM_END:
        # RAM does not move in this step. The word is still an address, and the
        # export carries it so that a later RAM move knows where they are.
        return "constant", "ram", "static or stack address; RAM is not moved yet"
    if not APP_BASE <= (target if thumb else value) < APP_END:
        if peripherals.in_peripheral_space(value):
            # The peripherals never move, so this stays a constant and the
            # linker is not involved; what the signal adds is the name, which
            # is what every reader of words.json was missing here.
            note = (peripheral_note(part.chip, word, uses)
                    if part.read_sites.get(word["addr"]) else None)
            if note is not None:
                return "constant", "peripheral", note
            number = peripherals.plausible_float(value)
            if number is not None:
                return "constant", "float_constant", "%g as a float" % number
        return "constant", "out_of_range", ""
    if thumb and target in part.functions:
        # Landing exactly on a function entry with bit 0 set survives the
        # stride question: a window over two fields would have to reproduce a
        # function's whole address, not merely land somewhere in the code, and
        # the record tables this image fragments across several items and gaps
        # hold their handlers exactly this way.
        return "pointer", "thumb_function_start", part.functions[target]["name"]
    if (value not in part.starts and part.item_of(value) is None
            and part.names_a_string(value)):
        # Ahead of the word-slot test, like a function start and for the same
        # reason: a window over two fields would have to reproduce the whole
        # address of a string's first byte.
        return "pointer", "names_a_string_run", ""
    cell = initialiser_cell(part, word["addr"])
    if cell:
        # Inside a copied image, and the RAM word this one becomes has a use
        # summary. Dereferenced or called is a pointer on the same evidence
        # execution gives; used and never dereferenced is a constant, and this
        # is the one place that half is safe to apply. Elsewhere "only compared"
        # was falsified because a sentinel test and a bound test are the same
        # instruction on a pointer -- but that argument needs a pointer to test,
        # and a word the code only ever compares or scales, reached through a
        # cell whose every reader is accounted for, has no such reading here:
        # the cell is keyed by the absolute address, so every load of it is in
        # the summary, which is not true of a value passed around in registers.
        if set(cell) & POINTER_USES:
            return "pointer", "ram_initialiser_mapping", \
                "the RAM word it becomes is used as %s" % ", ".join(sorted(set(cell) & POINTER_USES))
        return "constant", "ram_initialiser_mapping", \
            "the RAM word it becomes is only %s" % ", ".join(sorted(cell))
    if word["kind"] in ("data", "gap") and not part.is_slot(word["addr"]):
        # The word is a window over an object whose stride is not four, so what
        # it reads is two halves of two fields, not one value the compiler put
        # there. Out of range it does not matter; in range it would relocate the
        # middle of a record.
        return "review", "not_a_word_slot", word["kind"]
    if thumb and part.in_code(target):
        # A case body, a label in a split function, a resume point: the Thumb
        # bit plus code is as certain as a function start, only the symbol is
        # interior.
        return "pointer", "thumb_interior_code", ""
    # Bit 0 only means Thumb on something that is code. An odd value that is an
    # ordinary address of a byte -- the middle of a string, a record field -- is
    # far more common in this image than a function pointer is, so the plain
    # reading is tried before the masked one is given up on.
    if value in part.starts:
        return "pointer", "item_start", part.starts[value]
    if part.item_of(value) is not None:
        return "pointer", "interior_item", ""
    if value in part.in_body_words:
        return "pointer", "in_body_word", "a pool word inside a function body"
    if dereferenced:
        # The code branches to this value or reads memory through it, so it is
        # an address whatever the bytes around it look like. This fires ahead of
        # the code-range test on purpose: a value inside a function body that is
        # a load base is data the disassembly over-covered, which is the one way
        # the Thumb argument below can be wrong.
        return "pointer", "use_pointer", ",".join(sorted(dereferenced))
    if word["addr"] in part.copy_sources:
        name, dest, length = part.copy_sources[word["addr"]]
        return "pointer", "ram_initialiser_source", \
            "%s copies %d bytes from here to 0x%x" % (name, length, dest)
    if part.in_code(value):
        # This image is Thumb throughout (9 movt, none building an address, and
        # no ARM code), so an even address is not a way to name an instruction.
        # A word holding one is a number that collided with the code's range.
        return "constant", "interior_code_without_thumb", ""
    if value in part.functions:
        return "review", "function_start_without_thumb", part.functions[value]["name"]

    # What is left lands in one of the untyped runs: a record table's own row,
    # or a number, and nothing the code did with it settles which.
    return "review", "lands_in_untyped_run", ",".join(sorted(uses))


def owning_item(part, target):
    """(symbol address, addend) for a pointer, against the item it lands in."""
    if target in part.starts or target in part.functions:
        return target, 0
    lo, hi = 0, len(part.code)
    while lo < hi:
        mid = (lo + hi) // 2
        if part.code[mid][0] <= target:
            lo = mid + 1
        else:
            hi = mid
    if lo and part.code[lo - 1][1] > target:
        return part.code[lo - 1][2], target - part.code[lo - 1][2]
    item = part.item_of(target)
    if item is not None:
        return item, target - item
    return target, 0


def classify(items, refs, blob, contracts, overrides, tables, chip, cells=()):
    outside = sorted(set(overrides) - set(w["addr"] for w in refs["words"]))
    if outside:
        sys.exit("abi/words.yaml declares %d addresses the candidate set does"
                 " not offer, starting at 0x%x" % (len(outside), outside[0]))
    part = Partition(items, refs["words"], blob)
    part.chip = chip
    part.read_sites = collections.Counter(r["target"] for r in refs["pool_reads"])
    part.slots = slot_objects(items, blob, part)
    part.copy_sources = copy_initialisers(items, refs, blob, part)
    part.cells = dict(cells)
    by_addr = {w["addr"]: w for w in refs["words"]}
    part.copies = tuple((int.from_bytes(blob[at - APP_BASE:at - APP_BASE + 4],
                                        "little"), length, dest)
                        for at, (_, dest, length) in part.copy_sources.items()
                        if part.decides(by_addr[at], overrides))
    rows, buckets, review, displacements = [], {}, {}, []
    first = [dict(word, **dict(zip(("class", "signal", "note"),
                                  decide(word, part, contracts, overrides))))
             for word in refs["words"]]
    from_runs = runs.analyse(items, refs, first, blob, tables, part)
    for word in first:
        klass, signal, note = (from_runs.get(word["addr"])
                               or (word["class"], word["signal"], word["note"]))
        # `thumb_*` are the only signals that read bit 0 as the Thumb bit; for
        # every other one the value is the address, odd or not.
        target = (overrides[word["addr"]]["target"] if signal == "override"
                  and klass == "pointer"
                  else word["value"] & ~1 if signal.startswith("thumb_")
                  else word["value"])
        item, addend = (owning_item(part, target) if klass == "pointer"
                        else (0, 0))
        rows.append({"addr": word["addr"], "value": word["value"],
                     "kind": word["kind"], "class": klass, "signal": signal,
                     "note": note, "target": target if klass == "pointer" else 0,
                     "item": item, "addend": addend, "thumb": word["thumb"],
                     "in_code": klass == "pointer" and part.in_code(target),
                     "thumb_target": signal.startswith("thumb_")
                                     or (klass == "pointer" and target % 2 == 1),
                     "uses": word.get("uses") or [],
                     # How many bytes past the target are read as one block.
                     # A RAM initialiser image is copied in a single memcpy
                     # whose length is computed from two RAM addresses, so only
                     # its first byte is named and everything behind it is
                     # reached by being where it was: the run has to stay one
                     # piece however its items are cut up. Only where the copy
                     # shape is the deciding signal, which is the case its
                     # docstring argues for -- a word another rule decided is
                     # named by something else, and the two RAM addresses its
                     # function happens to hold measure out a length nothing
                     # says is this word's (0x50e8c and 0x50e94 both come out
                     # at 78904 bytes, which they cannot both be).
                     "span": part.copy_sources[word["addr"]][2]
                             if part.decides(word, overrides) else 0})
        for site in word.get("pc_sites") or ():
            displacements.append({"word": word["addr"], "site": site,
                                  "target": (word["value"] + site + 4) & 0xFFFFFFFF})
        key = "%s:%s:%s" % (word["kind"], klass, signal)
        buckets[key] = buckets.get(key, 0) + 1
        if klass == "review":
            review.setdefault(signal, []).append(rows[-1])
    return rows, buckets, review, displacements


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", default=os.path.join(HERE, "out", "ghidra"))
    ap.add_argument("--image", default=os.path.join(SIM, "appl.bin"))
    ap.add_argument("--facts", default=os.path.join(HERE, "words.yaml"))
    args = ap.parse_args()
    with open(os.path.join(args.export, "items.json")) as fh:
        items = json.load(fh)
    with open(os.path.join(args.export, "references.json")) as fh:
        refs = json.load(fh)
    with open(os.path.join(args.export, "word_uses.json")) as fh:
        flow_file = json.load(fh)
    flow = {r["addr"]: r for r in flow_file["uses"]}
    cells = {c["addr"]: sorted(c["uses"]) for c in flow_file.get("cells", ())}
    for word in refs["words"]:
        seen = flow.get(word["addr"])
        word["uses"] = sorted(seen["uses"]) if seen else []
        word["pc_sites"] = seen["pc_sites"] if seen else []

    with open(args.image, "rb") as fh:
        blob = fh.read()
    contracts, overrides = read_facts(args.facts, blob, items["functions"])
    tables = shapes.load().typed_regions(symbols.load())
    rows, buckets, review, displacements = classify(items, refs, blob, contracts,
                                                    overrides, tables,
                                                    peripherals.load(), cells)
    pointers = [r for r in rows if r["class"] == "pointer"]
    summary = {
        "candidates": len(rows),
        "pointers": len(pointers),
        "pointers_into_code": sum(1 for r in pointers if r["in_code"]),
        "constants": sum(1 for r in rows if r["class"] == "constant"),
        "review": sum(1 for r in rows if r["class"] == "review"),
        "overrides": len(overrides),
        "buckets": buckets,
        "review_buckets": {k: len(v) for k, v in sorted(review.items())},
        "displacements": len(displacements),
    }
    path = os.path.join(args.export, "words.json")
    with open(path, "w") as fh:
        fh.write('{"_generated": %s,\n"summary": %s,\n"displacements": %s,'
                 '\n"words": ['
                 % (json.dumps("by abi/classify_words.py, do not edit: pointer"
                               " versus constant for every candidate word"),
                    json.dumps(summary), json.dumps(displacements)))
        for i, row in enumerate(rows):
            fh.write("%s\n%s" % ("," if i else "", json.dumps(row)))
        fh.write("]}\n")

    print("%s: %d candidates, %d pointers (%d into code), %d constants, %d to"
          " review"
          % (path, summary["candidates"], summary["pointers"],
             summary["pointers_into_code"], summary["constants"], summary["review"]))
    for key in sorted(buckets):
        print("  %-48s %d" % (key, buckets[key]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
