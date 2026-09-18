#!/usr/bin/env python3
"""Refuse a link line that defines one name twice.

The partition names a blob body after the library function it is a copy of, so
a name the objectified app publishes that an archive on the link line also
defines is two definitions of one name. The link only fails when something else
pulls that member in for another symbol, which is why it showed up as
`multiple definition of __aeabi_dadd` in one configuration and nowhere else:
the name is an interior label of the blob's `__subdf3` section and libgcc's
`_arm_addsubdf3.o` defines it too. abi/blobify.py's --reserve-defs is the fix;
this is the check that the fix covered everything the link sees.

    abi/link_defs.py --archive <archive>... <object>...
"""
import argparse
import os
import subprocess
import sys


def defines(path, tools):
    """Every name a relocatable or an archive defines, and where it defines it."""
    listing = subprocess.run([tools + "nm", "-A", path], capture_output=True,
                             text=True, check=True).stdout
    found = {}
    for line in listing.splitlines():
        head, _, rest = line.rpartition(":")
        row = rest.split()
        # A weak or common definition loses to a strong one rather than
        # colliding with it, and an undefined symbol is the whole point.
        if len(row) < 2 or row[-2] in ("U", "u", "w", "v", "V", "W"):
            continue
        member = head.rpartition(":")[2]
        found.setdefault(row[-1], "%s(%s)" % (os.path.basename(path), member)
                         if member != path else os.path.basename(path))
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("objects", nargs="+", help="the link line's relocatables")
    ap.add_argument("--archive", action="append", default=[], metavar="PATH",
                    help="an archive the link line searches")
    ap.add_argument("--tools", default="arm-none-eabi-")
    args = ap.parse_args()
    objects, archives = args.objects, args.archive

    published = {}
    for path in objects:
        for name, where in defines(path, args.tools).items():
            published.setdefault(name, where)
    clashes = []
    for path in archives:
        for name, where in defines(path, args.tools).items():
            if name in published:
                clashes.append((name, published[name], where))
    if clashes:
        for name, mine, theirs in sorted(clashes)[:20]:
            print("%s is defined in %s and in %s" % (name, mine, theirs))
        sys.exit("%d names the link line defines twice" % len(clashes))
    print("no name is defined twice on the link line (%d objects, %d archives)"
          % (len(objects), len(archives)))


if __name__ == "__main__":
    main()
