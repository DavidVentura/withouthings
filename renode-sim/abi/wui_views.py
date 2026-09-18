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


def defaults(img, found):
    """slot -> (address, how many vtables share it, how many of those log it).

    An implementation is a default when the vtables that share it are the
    plurality at that slot and it logs the slot's own role string.
    """
    by_slot = {}
    for _, vtable, _ in found:
        for i in range(16):
            value = img.word(vtable + 4 * i)
            if not (APP_BASE < value < APP_END and value & 1):
                break
            by_slot.setdefault(4 * i, {})
            by_slot[4 * i][value & ~1] = by_slot[4 * i].get(value & ~1, 0) + 1
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
    for role, (addr, n, total) in sorted(defaults(img, found).items()):
        out.append({"address": addr, "name": "wui_view_default_" + role,
                    "kind": "function", "class": CLASS, "module": "wui",
                    "evidence": "the %d of %d view vtables that share this slot"
                                " implementation log \"%s\" through the"
                                " \"[WUI] %%s %%s\" line, which is the"
                                " firmware's own word for the slot"
                                % (n, total, role)})
    return out


def main():
    img = Image(os.path.join(SIM, "appl.bin"))
    prior = symmap.load()
    found = rows(img, {sym.address for sym in prior.symbols
                       if sym.klass != CLASS})
    try:
        added = symmap.load().rewrite(found, {CLASS})
    except symmap.Refusal as err:
        sys.exit("abi/wui_views.py: abi/symbols.yaml: %s" % err)
    print("%d view descriptors, %d slot implementations; %d entries added"
          % (sum(1 for r in found if r["kind"] == "global"),
             sum(1 for r in found if r["kind"] == "function"), added))


if __name__ == "__main__":
    main()
