#!/usr/bin/env python3
"""Refuse a relink whose library differs from the image's in anything but placement.

    python3 abi/lib_parity.py --elf out/relink/relinked.elf --image out/flash-relinked.bin

abi/matches.yaml can pass a body with a masked pool word or a call it could not
resolve, and says nothing about a body it never matched. This compares what was
actually linked: every function in the library region is paired with the image's
body, and the two must agree on every instruction, with a call or an address
counting as equal only when it names the same thing on both sides. Strings the
library points at and the statics it starts with are compared too, and in an
unmoved link everything outside the library must differ only by those references.
"""

import argparse
import os
import re
import struct
import subprocess
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
ROOT = os.environ.get("ROOT", os.path.expanduser("~/ref-build"))
NM = os.path.join(ROOT, "tc/arm-gnu-toolchain-13.2.Rel1-x86_64-arm-none-eabi/bin/arm-none-eabi-nm")
APP_START = 0x27000
LIBRARY_START = 0xf1180
LIBRARY_END = 0xfc000
FLASH_END = 0x100000
# The startup's copy of .data, at 0x36e1c: its end, its start and where it loads from.
STOCK_DATA_POOL = (0x36e3c, 0x36e38, 0x36e40)
STOCK_HEAP = 0x20027248
ALIAS = re.compile(r"^[aD]_([0-9a-f]{8})$")


def read_symbols(elf):
    """Functions by name, and the address each alias of a stock address now has."""
    functions, aliases, names = {}, {}, {}
    for line in subprocess.check_output([NM, "-S", "--defined-only", elf], text=True).splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        address, name = int(fields[0], 16), fields[-1]
        names.setdefault(address & ~1, name)
        alias = ALIAS.match(name)
        if alias:
            aliases[int(alias.group(1), 16)] = address & ~1
        if len(fields) == 4 and fields[2] in "tT" and LIBRARY_START <= address < LIBRARY_END:
            if name in functions:
                sys.exit("%s is in the library twice, at %#x and %#x, and only one copy could be compared"
                         % (name, functions[name][0], address & ~1))
            functions[name] = (address & ~1, int(fields[1], 16))
    return functions, aliases, names


def heap_is_where_the_image_has_it(elf):
    for line in subprocess.check_output([NM, elf], text=True).splitlines():
        fields = line.split()
        if fields[-1] == "ucHeap":
            return int(fields[0], 16) == STOCK_HEAP
    return False


def c_string(image, at):
    return bytes(image[at:image.index(0, at)])


def check_strings(stock, linked, parity):
    """A pointer into the library's own constants points at the same text."""
    failed = False
    for stock_word, linked_word in sorted(parity.words.items()):
        if stock_word >> 24 == 0x20 or stock_word & 1 or not LIBRARY_START <= linked_word < LIBRARY_END:
            continue
        before, after = c_string(stock, stock_word), c_string(linked, linked_word)
        if before != after:
            failed = True
            print("the string at %#x is %r in the image and %r here" % (stock_word, before, after))
    return failed


def read_library_data_bounds(elf):
    wanted = ("__libdata_start", "__libdata_end", "__libdata_load")
    found = {}
    for line in subprocess.check_output([NM, elf], text=True).splitlines():
        fields = line.split()
        if fields[-1] in wanted and len(fields) == 3:
            found[fields[-1]] = int(fields[0], 16)
    return tuple(found[name] for name in wanted)


def known_pairs(functions):
    with open(os.path.join(HERE, "boundary.yaml")) as fh:
        boundary = yaml.safe_load(fh)
    with open(os.path.join(HERE, "matches.yaml")) as fh:
        matches = yaml.safe_load(fh)
    stock = {}
    for f in matches["functions"]:
        stock[f["name"]] = f["address"]
    for e in boundary["app_to_lib"]:
        if e.get("symbol") and not e.get("keep"):
            stock[e["symbol"]] = e["addr"]
    for v in boundary["data_references"]["vector_table"]:
        if v.get("relocate"):
            stock[v["symbol"]] = v["word"] & ~1
    return boundary, {stock[n]: n for n in functions if n in stock}


def long_branch(image, at):
    """The target of a BL or B.W at `at`, or None."""
    high, low = struct.unpack_from("<HH", image, at)
    if high >> 11 != 0x1e or low >> 14 not in (2, 3) or (low >> 14 == 2 and not low >> 12 & 1):
        return None
    sign = high >> 10 & 1
    offset = (sign << 24) | ((1 - ((low >> 13 & 1) ^ sign)) << 23) | ((1 - ((low >> 11 & 1) ^ sign)) << 22) \
        | ((high & 0x3ff) << 12) | ((low & 0x7ff) << 1)
    target = at + 4 + (offset - (1 << 25) if offset >> 24 else offset)
    # The second half of a 32-bit instruction followed by a first half can decode
    # as a branch too, and lands nowhere.
    return target if 0 <= target < FLASH_END else None


def is_address(word):
    return 0x1000 <= word < 0x100000 or word >> 24 == 0x20


class Parity:
    def __init__(self, stock, linked, functions, aliases, names, pairs):
        self.stock, self.linked = stock, linked
        self.functions, self.aliases, self.names = functions, aliases, names
        self.pairs = pairs
        self.by_name_address = {address: name for name, (address, _) in functions.items()}
        self.words, self.words_back = {}, {}
        self.problems = {}

    def containing(self, address):
        for name, (start, size) in self.functions.items():
            if start <= address < start + size:
                return name, address - start
        return None, 0

    def same_call(self, stock_target, linked_target):
        if self.aliases.get(stock_target) == linked_target:
            return None
        partner, offset = self.containing(linked_target)
        if partner is None:
            return "calls %#x in the image and %s here" % (stock_target, self.names.get(linked_target, hex(linked_target)))
        known = next((a for a, n in self.pairs.items() if n == partner), None)
        if known is None and offset:
            known = stock_target - offset
            self.pairs[known] = partner
        if offset:
            if known + offset != stock_target:
                return "calls %s+%#x, which is %#x in the image and not %#x" % (partner, offset, known + offset, stock_target)
            return None
        if self.pairs.setdefault(stock_target, partner) != partner:
            return "calls %s through %#x, which is %s" % (partner, stock_target, self.pairs[stock_target])
        return None

    def same_word(self, name, offset, stock_word, linked_word):
        """Two address words agree when each side always names the same thing."""
        if self.aliases.get(stock_word & ~1) == linked_word & ~1:
            return True
        if self.words.setdefault(stock_word, linked_word) != linked_word:
            return False
        self.words_back.setdefault(linked_word, []).append((stock_word, name, offset))
        return True

    def ambiguous_words(self):
        """Words where two image addresses became one here.

        An object's end is the same address as whatever the link put next, so
        ucHeap + size can land on uxCriticalNesting: that is not a merge when the
        word is the same distance from another address paired the same way.
        """
        found = []
        pairs = list(self.words.items())
        for linked, uses in self.words_back.items():
            displaced = set(s for s, _, _ in uses
                            if any(other != s and l != linked and s - other == linked - l for other, l in pairs))
            remaining = [use for use in uses if use[0] not in displaced]
            if len(set(s for s, _, _ in remaining)) > 1:
                found += [(name, offset, stock, linked) for stock, name, offset in remaining]
        return found

    def compare(self, name, stock_at):
        at, size = self.functions[name]
        return self.walk(name, stock_at, at, size)

    def walk(self, name, stock_at, at, size):
        problems = []
        offset = 0
        while offset + 1 < size:
            here, there = stock_at + offset, at + offset
            if there % 4 == 0 and offset + 3 < size:
                stock_word = struct.unpack_from("<I", self.stock, here)[0]
                linked_word = struct.unpack_from("<I", self.linked, there)[0]
                if stock_word != linked_word and is_address(stock_word) and is_address(linked_word):
                    if not self.same_word(name, offset, stock_word, linked_word):
                        problems.append("+%#x address %#x is %s here" % (
                            offset, stock_word, self.names.get(linked_word & ~1, hex(linked_word))))
                    offset += 4
                    continue
            if self.stock[here:here + 2] == self.linked[there:there + 2]:
                target = long_branch(self.stock, here) if offset + 3 < size else None
                if target is not None:
                    same = self.stock[here + 2:here + 4] == self.linked[there + 2:there + 4]
                    if not same:
                        linked_target = long_branch(self.linked, there)
                        problem = linked_target is None and "a call became something else" or self.same_call(target, linked_target)
                        if problem:
                            problems.append("+%#x %s" % (offset, problem))
                        offset += 4
                        continue
                offset += 2
                continue
            target = long_branch(self.stock, here) if offset + 3 < size else None
            linked_target = long_branch(self.linked, there) if offset + 3 < size else None
            if target is not None and linked_target is not None:
                problem = self.same_call(target, linked_target)
                if problem:
                    problems.append("+%#x %s" % (offset, problem))
                offset += 4
                continue
            problems.append("+%#x instruction differs: %s against %s" % (
                offset, self.stock[here:here + 2].hex(), self.linked[there:there + 2].hex()))
            offset += 2
        return problems

    def run(self):
        done = set()
        while True:
            todo = [(address, name) for address, name in self.pairs.items() if address not in done]
            if not todo:
                return
            for address, name in todo:
                done.add(address)
                problems = self.compare(name, address)
                if problems:
                    self.problems[name] = problems


def check_outside_library(stock, linked, parity):
    """What the library did not change, an unmoved link must leave alone."""
    failed = False
    for start, end in ((0, APP_START), (LIBRARY_END, min(len(stock), len(linked)))):
        if stock[start:end] != linked[start:end]:
            failed = True
            print("%#x..%#x differs from the image, which the relink has no reason to touch" % (start, end))
    problems = parity.walk("app", APP_START, APP_START, LIBRARY_START - APP_START)
    for problem in problems[:40]:
        print("app " + problem)
    return failed or bool(problems)


def check_initial_values(stock, linked, parity, aliases_of_library):
    """A static the kernel reads before it writes starts the same on both sides."""
    start, end, load = (struct.unpack_from("<I", stock, at)[0] for at in STOCK_DATA_POOL)
    linked_start, linked_end, linked_load = aliases_of_library
    failed = False
    for stock_word, linked_word in sorted(parity.words.items()):
        if stock_word >> 24 != 0x20 or linked_word >> 24 != 0x20:
            continue
        before = struct.unpack_from("<I", stock, load + stock_word - start)[0] if start <= stock_word < end else 0
        after = 0
        if linked_start <= linked_word < linked_end:
            after = struct.unpack_from("<I", linked, linked_load + linked_word - linked_start)[0]
        if before != after and linked_word != linked_end:
            failed = True
            print("%#x starts as %#x in the image and %#x here" % (stock_word, before, after))
    return failed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--elf", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--stock", default=os.path.join(SIM, "flash.bin"))
    ap.add_argument("--placement-moved", action="store_true",
                    help="the link moved or replaced things, so only the bodies paired with the image are compared")
    args = ap.parse_args()

    functions, aliases, names = read_symbols(args.elf)
    boundary, pairs = known_pairs(functions)
    with open(args.stock, "rb") as fh:
        stock = fh.read()
    with open(args.image, "rb") as fh:
        linked = fh.read()
    parity = Parity(stock, linked, functions, aliases, names, pairs)
    parity.run()

    exceptions = boundary.get("parity_exceptions", {})
    paired = set(parity.pairs.values())
    failed = False
    for name, problems in sorted(parity.problems.items()):
        if name in exceptions:
            continue
        failed = True
        print("%s differs from the image's %#x:" % (name, next(a for a, n in parity.pairs.items() if n == name)))
        for problem in problems[:6]:
            print("    " + problem)
        if len(problems) > 6:
            print("    and %d more" % (len(problems) - 6))
    for name in sorted(() if args.placement_moved else set(functions) - paired - set(exceptions)):
        failed = True
        print("%s is in the library and has no body in the image to answer to" % name)
    for name in sorted(exceptions):
        if name not in parity.problems and name in paired:
            failed = True
            print("%s is listed as an exception and agrees with the image" % name)
    for name, offset, stock_word, linked_word in parity.ambiguous_words():
        failed = True
        print("%s+%#x: %#x is %#x here, which another image address also became" % (name, offset, stock_word, linked_word))
    failed = check_strings(stock, linked, parity) or failed
    failed = check_initial_values(stock, linked, parity, read_library_data_bounds(args.elf)) or failed
    if not args.placement_moved and not heap_is_where_the_image_has_it(args.elf):
        failed = True
        print("the heap is not at %#x, and the app's stack dump writes into it by address" % STOCK_HEAP)
    if not args.placement_moved:
        failed = check_outside_library(stock, linked, parity) or failed
    print("library parity: %d bodies, %d exceptions" % (len(paired), len(exceptions)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
