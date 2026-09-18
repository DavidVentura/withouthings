#!/usr/bin/env python3
"""What one archive body costs the link that takes it, in archive bytes.

    python3 abi/archive_cost.py --archive <a.a> [--archive <b.a>] \
        --root memcpy --root sscanf [--base-object out/relink/tasks.o]

Replacing a blob body with the archive's is not free: the linker pulls in the
member that defines it, then every member that member's undefined symbols
reach. A body of 22 bytes whose closure is 15 KB of archive code the image
never had is a loss, and the loss is invisible in the verdict that says the
source reproduces the body.

The marginal cost of a root is the archive text the link would not carry
without it: the closure of the whole root set minus the closure of the root
set without it. Measured that way a body shared by two roots is charged to
neither, which is what makes the number a decision: dropping the root really
does return that many bytes.

The walk is the linker's own rule -- a member joins when an undefined symbol
names one of its definitions, archives are searched in the order given -- run
over `nm` output rather than over a link, so it needs no placement and no
successful link to answer.
"""

import argparse
import collections
import os
import subprocess
import sys


class Archives(object):
    """Members of a list of archives, by the symbols they define and need."""

    def __init__(self, paths, tools=""):
        self.defines = {}                       # symbol -> member key
        self.needs = collections.defaultdict(set)
        self.text = collections.Counter()
        self.order = []
        for path in paths:
            self._index(path, tools)

    def _index(self, path, tools):
        listing = subprocess.run([tools + "nm", "-A", path],
                                 capture_output=True, text=True).stdout
        for line in listing.splitlines():
            head, _, rest = line.rpartition(":")
            member = (path, head.rpartition(":")[2])
            row = rest.split()
            if len(row) < 2:
                continue
            kind, name = row[-2], row[-1]
            if member not in self.text:
                self.text[member] = 0
                self.order.append(member)
            if kind == "U":
                self.needs[member].add(name)
            elif kind not in ("u", "w", "v"):
                self.defines.setdefault(name, member)
        sizes = subprocess.run([tools + "size", path],
                               capture_output=True, text=True).stdout
        for line in sizes.splitlines()[1:]:
            row = line.split()
            if len(row) < 6:
                continue
            member = (path, row[5])
            if member in self.text:
                self.text[member] = int(row[0]) + int(row[1])

    def closure(self, symbols):
        """Every member the linker would pull in for a set of undefined symbols."""
        pulled, pending = set(), list(symbols)
        while pending:
            symbol = pending.pop()
            member = self.defines.get(symbol)
            if member is None or member in pulled:
                continue
            pulled.add(member)
            pending.extend(self.needs[member])
        return pulled

    def bytes_of(self, members):
        return sum(self.text[m] for m in members)

    def marginal(self, roots):
        """root -> (bytes, members) the link carries only because of that root."""
        roots = list(roots)
        whole = self.closure(roots)
        out = {}
        for root in roots:
            without = self.closure([r for r in roots if r != root])
            only = whole - without
            out[root] = (self.bytes_of(only), sorted(m[1] for m in only))
        return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", action="append", required=True)
    ap.add_argument("--root", action="append", default=[])
    ap.add_argument("--tools", default="arm-none-eabi-")
    ap.add_argument("--list", type=int, default=20)
    args = ap.parse_args()

    archives = Archives(args.archive, args.tools)
    cost = archives.marginal(args.root)
    total = archives.bytes_of(archives.closure(args.root))
    print("%d roots pull in %d archive text+data bytes in total" % (len(args.root), total))
    for root, (size, members) in sorted(cost.items(), key=lambda kv: -kv[1][0])[:args.list]:
        print("  %-24s %6d B  %s" % (root, size, " ".join(members[:6])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
