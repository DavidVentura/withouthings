#!/usr/bin/env python3
"""The WUI view descriptors and the shared slot implementations they point at.

    python3 abi/wui_views.py            # writes the wuiview class of abi/symbols.yaml

Every descriptor carries its own name as its second word -- the string the
"[WUI] %s %s" line prints -- so the image names all but one of them and this
only has to find them. A descriptor is a word that points at a vtable (three
Thumb function starts in 0xb9ff4..0xbc000) followed by a pointer to a printable
string, and the run of them is 0xb91cc..0xb9fcc plus one more at 0xba048.

The shared defaults are named by role rather than by address, and the role is
the firmware's own word for it: each of the four lifecycle slots logs its role
string, so the implementation the most vtables share at that slot is
wui_view_default_<role> and the string in its own literal pool is the evidence.

Nothing here is a guess: a descriptor with no name string is not named, and a
slot no implementation logs a role for gets no default name.
"""

import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import symbols as symmap  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
APP_BASE = 0x27000
APP_END = 0xF117C
# The run the descriptors live in and the run their vtables live in. Both are
# read off the image: below 0xb91cc the words are not vtable pointers and the
# first vtable is above the last descriptor.
DESC_LO, DESC_HI = 0xB9000, 0xBA200
VT_LO, VT_HI = 0xB9FF4, 0xBC000

# The role each string is the firmware's word for, and the slot the callers put
# it at. See struct wui_view_vtable in abi/include/withings/wui.h.
ROLE_STRINGS = {0xE6C09: ("on_enter", 0x04),
                0xE6C4A: ("on_exit", 0x08),
                0xE6B94: ("on_foreground", 0x0C),
                0xE6B62: ("on_background", 0x10)}

CLASS = "wuiview"

# The wlog entry points a slot body can tail-call, from abi/autonames.py's
# WLOG_FMT_REG; only the four that take the format in r0 can be tail-called
# with the format already in place.
WLOG_ENTRIES = frozenset((0x8F460, 0x8F494, 0x9B1C8, 0x5E7A8))

# The slots a caller fixes the role of, from struct wui_view_vtable. A slot no
# caller fixes gets no name, here or in the header.
SLOT_ROLE = {0x00: "on_event", 0x04: "on_enter", 0x08: "on_exit",
             0x0C: "on_foreground", 0x10: "on_background", 0x14: "refresh"}


class Image(object):
    def __init__(self, path):
        with open(path, "rb") as fh:
            self.data = fh.read()

    def word(self, at):
        return struct.unpack_from("<I", self.data, at - APP_BASE)[0]

    def half(self, at):
        return struct.unpack_from("<H", self.data, at - APP_BASE)[0]

    def text(self, at):
        """A string that may hold the whitespace a log line ends with."""
        if not APP_BASE <= at < APP_END:
            return None
        end = self.data.find(b"\0", at - APP_BASE)
        if end < 0:
            return None
        raw = self.data[at - APP_BASE:end]
        if not raw or any(not (32 <= c < 127 or c in (9, 10, 13))
                          for c in bytearray(raw)):
            return None
        return raw.decode()

    def string(self, at):
        if not APP_BASE <= at < APP_END:
            return None
        end = self.data.find(b"\0", at - APP_BASE)
        if end < 0:
            return None
        text = self.data[at - APP_BASE:end]
        if not text or any(not 32 <= c < 127 for c in bytearray(text)):
            return None
        return text.decode()

    def loads(self, entry, value):
        """Does the body at `entry` hold `value` in its own literal pool?

        A Thumb function's pool sits inside or immediately after its body and
        the four defaults are all short, so the word is within a page of the
        entry; beyond that the answer would be some other function's pool.
        """
        want = struct.pack("<I", value)
        window = self.data[entry - APP_BASE:entry - APP_BASE + 0x200]
        return want in window

    def is_vtable(self, at):
        if not VT_LO <= at < VT_HI:
            return False
        return all(APP_BASE < self.word(at + 4 * i) < APP_END
                   and self.word(at + 4 * i) & 1 for i in range(3))


def descriptors(img):
    """(address, vtable, name or None) for every view descriptor, in order."""
    found = []
    for at in range(DESC_LO, DESC_HI, 4):
        vtable = img.word(at)
        if img.is_vtable(vtable):
            found.append((at, vtable, img.string(img.word(at + 4))))
    return found


def identifier(name, taken):
    base = "wui_view_" + (re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower())
    # Four names are carried by two descriptors each ("SPO2 meas", "Missing
    # Medical Permissions", and the carousels' wrapper-plus-inner pairs), which
    # is the image's doing and not a collision to resolve by picking one.
    taken[base] = taken.get(base, 0) + 1
    return base if taken[base] == 1 else "%s_%d" % (base, taken[base])


def slot_counts(img, found):
    """slot -> {implementation: how many view vtables hold it there}."""
    by_slot = {}
    for _, vtable, _ in found:
        for i in range(16):
            value = img.word(vtable + 4 * i)
            if not (APP_BASE < value < APP_END and value & 1):
                break
            by_slot.setdefault(4 * i, {})
            by_slot[4 * i][value & ~1] = by_slot[4 * i].get(value & ~1, 0) + 1
    return by_slot


def defaults(img, found):
    """slot -> (address, how many vtables share it, how many of those log it).

    An implementation is a default when the vtables that share it are the
    plurality at that slot and it logs the slot's own role string.
    """
    by_slot = slot_counts(img, found)
    out = {}
    for target, (role, slot) in ROLE_STRINGS.items():
        counts = by_slot.get(slot, {})
        if not counts:
            continue
        addr, n = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))
        if not img.loads(addr, target):
            continue
        out[role] = (addr, n, sum(counts.values()))
    return out


def thumb_literal(img, at):
    """The pool word a 16-bit `ldr rN,[pc,#imm8]` at `at` loads, and its register."""
    half = img.half(at)
    if half >> 11 != 0b01001:
        return None, None
    pool = ((at + 4) & ~3) + (half & 0xFF) * 4
    return (half >> 8) & 7, img.word(pool)


def thumb_wide_branch(img, at):
    """The target of a 32-bit `b.w` or `bl` at `at`, or None if it is neither."""
    first, second = img.half(at), img.half(at + 2)
    if first >> 11 != 0b11110 or (second >> 14) != 0b10 or not (second >> 12) & 1:
        return None
    sign = (first >> 10) & 1
    j1, j2 = (second >> 13) & 1, (second >> 11) & 1
    offset = (sign << 24) | ((1 - (j1 ^ sign)) << 23) | ((1 - (j2 ^ sign)) << 22) \
        | ((first & 0x3FF) << 12) | ((second & 0x7FF) << 1)
    if sign:
        offset -= 1 << 25
    return at + 4 + offset


def log_only_slots(img, found, held):
    """Implementations whose whole body is the slot's own log line.

    The four bodies `defaults` finds are the plurality at their slot, and the
    image has a second family of the same thing: four instructions that load
    the "[WUI] %s %s" format and a fixed role literal, put the view's own name
    string in the third argument and tail-call the logger. A body like that
    does nothing but announce the slot it is in, so it is a default for that
    slot whichever vtables hold it, and the role it prints is the firmware's
    own word for which slot that is. Only a body several vtables share is
    named, because one vtable holding it makes it that view's own.
    """
    counts = {}
    for slot_impls in slot_counts(img, found).values():
        for addr, n in slot_impls.items():
            counts[addr] = counts.get(addr, 0) + n
    out, taken = [], {}
    for addr in sorted(counts):
        if counts[addr] < 2 or addr in held:
            continue
        role = logged_role(img, addr)
        if role is None:
            continue
        name = "wui_default_" + role
        taken[name] = taken.get(name, 0) + 1
        if taken[name] > 1:
            name = "%s_%d" % (name, taken[name] - 1)
        out.append((addr, name, role, counts[addr]))
    return out


def logged_role(img, addr):
    """The role a four-instruction log-only slot body prints, or None.

        ldr r2, [r0, #4]        the view descriptor's own name string
        ldr r1, [pc, #..]       the role literal, which is the slot
        ldr r0, [pc, #..]       the "[WUI] %s %s" format
        b.w  wlog

    Anything else in the body means the body does something, and a body that
    does something is not a default.
    """
    if img.half(addr) != 0x6842:          # ldr r2, [r0, #4]
        return None
    reg1, role_word = thumb_literal(img, addr + 2)
    reg0, fmt_word = thumb_literal(img, addr + 4)
    if reg1 != 1 or reg0 != 0:
        return None
    if thumb_wide_branch(img, addr + 6) not in WLOG_ENTRIES:
        return None
    if role_word not in ROLE_STRINGS:
        return None
    fmt = img.text(fmt_word)
    if fmt is None or fmt.count("%s") != 2:
        return None
    return ROLE_STRINGS[role_word][0]


def slot_owners(img, found, named):
    """address -> (slot, the one descriptor name) for a per-view implementation.

    A body several views share is named by none of them: the set of views that
    share it is the thing that has a name, and the firmware gives that set one
    only where it is a screen family, which nothing in the image says here. So
    only a body exactly one descriptor's vtable holds is named, and only at a
    slot whose role a caller fixed.
    """
    holders = {}
    for at, vtable, name in found:
        if name is None:
            continue
        for slot, role in SLOT_ROLE.items():
            value = img.word(vtable + slot)
            if not (APP_BASE < value < APP_END and value & 1):
                continue
            holders.setdefault((slot, value & ~1), set()).add(named[at])
    out = {}
    for (slot, addr), views in holders.items():
        if len(views) == 1:
            out[addr] = (slot, views.pop())
    return out


def rows(img, held=frozenset()):
    found = descriptors(img)
    taken, out, named = {}, [], {}
    for at, vtable, name in found:
        if name is None:
            continue
        named[at] = identifier(name, taken)
        out.append({"address": at, "name": named[at],
                    "kind": "global", "class": CLASS, "module": "wui",
                    "evidence": "the descriptor's own name string (%r at 0x%x),"
                                " which the \"[WUI] %%s %%s\" line prints; its"
                                " vtable is 0x%x"
                                % (name, img.word(at + 4), vtable)})
    for addr, (slot, view) in sorted(slot_owners(img, found, named).items()):
        if addr in held:
            # The map already names this body from its own evidence -- its
            # __func__, or a body match -- and where it sits in one vtable is
            # not a better name than the one Withings wrote.
            continue
        out.append({"address": addr,
                    "name": "%s_%s" % (SLOT_ROLE[slot], view[len("wui_view_"):]),
                    "kind": "function", "class": CLASS, "module": "wui",
                    "evidence": "the only view vtable holding this at +0x%02x"
                                " is %s's" % (slot, view)})
    plurality = defaults(img, found)
    for addr, name, role, n in log_only_slots(
            img, found, held | {a for a, _, _ in plurality.values()}):
        out.append({"address": addr, "name": name,
                    "kind": "function", "class": CLASS, "module": "wui",
                    "evidence": "the %d view vtables that share this hold a body"
                                " whose whole work is the \"[WUI] %%s %%s\" line"
                                " with the fixed slot literal \"%s\", so it"
                                " announces the slot and does nothing else"
                                % (n, role)})
    for role, (addr, n, total) in sorted(plurality.items()):
        out.append({"address": addr, "name": "wui_view_default_" + role,
                    "kind": "function", "class": CLASS, "module": "wui",
                    "evidence": "the %d of %d view vtables that share this slot"
                                " implementation log \"%s\" through the"
                                " \"[WUI] %%s %%s\" line, which is the"
                                " firmware's own word for the slot"
                                % (n, total, role)})
    return out


# ------------------------------------------------------- the vtables as objects
#
# The region every vtable a descriptor points at lives in, and which holds
# nothing else: the tables this finds tile 0xb9030..0xbc000 with no byte of any
# other object between one and the next.
VTABLE_LO, VTABLE_HI = 0xB9000, 0xBC000

# The struct a vtable of `n` handler words is declared as in
# abi/include/withings/wui.h. Six is the base kind the header already had.
SLOT_COUNT = len(SLOT_ROLE)


def vtable_type(slots):
    return "struct wui_view_vtable%s" % ("" if slots == SLOT_COUNT
                                         else "_%d" % slots)


def is_handler(value):
    return bool(value & 1) and APP_BASE < value < APP_END


def vtable_heads(img):
    """Every address in the region something points at as a vtable.

    A vtable is entered only through the word that holds its address, so the
    heads are values of the image's own words and not a grid laid over the
    region. Three handler words in a row is what tells a vtable from a pointer
    at anything else there: the region also holds the descriptors, the RAM
    pointers they carry and the drawing parameters between them.
    """
    heads = set()
    for at in range(APP_BASE, APP_END - 3, 4):
        value = img.word(at)
        if (VTABLE_LO <= value < VTABLE_HI - 8 and not value % 4
                and all(is_handler(img.word(value + 4 * i)) for i in range(3))):
            heads.add(value)
    return sorted(heads)


def vtables(img):
    """(head, slot count) for every vtable, in order.

    The count is the run of handler words the head opens, stopped at the next
    head: the region is dense, so where one table ends another begins, and the
    trailing NULL words 107 of them carry belong to no declaration -- a slot a
    table holds as NULL says nothing about what the slot is, and the word is
    not what names anything.
    """
    heads = vtable_heads(img)
    out = []
    for head, nxt in zip(heads, heads[1:] + [VTABLE_HI]):
        slots = 0
        while head + 4 * slots < nxt and is_handler(img.word(head + 4 * slots)):
            slots += 1
        out.append((head, slots))
    return out


def vtable_owner(img, head):
    """(descriptor address, view name) for the descriptor that points at `head`.

    A descriptor carries its own name as its second word -- the string the
    "[WUI] %s %s" line prints -- so the table is named by the view that owns
    it. Where two descriptors of different names share a table the first by
    address names it and the other is carried in the evidence; where no
    descriptor names it, nothing here does.
    """
    for at in range(VTABLE_LO, VTABLE_HI, 4):
        if img.word(at) != head:
            continue
        name = img.string(img.word(at + 4))
        if name:
            return at, name
    return None, None


def vtable_rows(img):
    """The declared vtable instances, and the heads no descriptor names."""
    taken, out, refused = {}, [], []
    for head, slots in vtables(img):
        at, name = vtable_owner(img, head)
        if name is None:
            refused.append((head, slots))
            continue
        ident = identifier(name, taken).replace("wui_view_", "wui_vtable_", 1)
        out.append({"address": head, "name": ident,
                    "kind": "global", "class": CLASS, "module": "wui",
                    "type": vtable_type(slots), "slots": slots,
                    "evidence": "the vtable of the view descriptor at 0x%x,"
                                " whose own name string is %r; %d handler words"
                                " before the next vtable at 0x%x"
                                % (at, name, slots, head + 4 * slots)})
    return out, refused


def header_block(img):
    """The declarations abi/include/withings/wui.h carries for the vtables."""
    found, _ = vtable_rows(img)
    lines = []
    for row in sorted(found, key=lambda r: r["name"]):
        lines.append("extern %s %s;" % (row["type"], row["name"]))
    return "\n".join(lines)


def main():
    img = Image(os.path.join(SIM, "appl.bin"))
    if "--header" in sys.argv:
        print(header_block(img))
        return
    prior = symmap.load()
    found = rows(img, {sym.address for sym in prior.symbols
                       if sym.klass != CLASS})
    instances, refused = vtable_rows(img)
    for head, slots in refused:
        print("abi/wui_views.py: 0x%x (%d slots) is a vtable no descriptor"
              " names, so nothing declares it" % (head, slots))
    found += [dict((k, v) for k, v in row.items() if k not in ("type", "slots"))
              for row in instances]
    try:
        added = symmap.load().rewrite(found, {CLASS},
                                      verified=set(r["address"] for r in found))
    except symmap.Refusal as err:
        sys.exit("abi/wui_views.py: abi/symbols.yaml: %s" % err)
    print("%d view descriptors, %d vtables (%d refused), %d slot"
          " implementations; %d entries added"
          % (sum(1 for r in found if r["kind"] == "global") - len(instances),
             len(instances), len(refused),
             sum(1 for r in found if r["kind"] == "function"), added))


if __name__ == "__main__":
    main()
