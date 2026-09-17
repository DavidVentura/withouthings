#!/usr/bin/env python3
"""Check a newlib build against the libc bodies the image carries.

    python3 abi/libc_check.py [--build newlib-nano-big]

abi/autonames.py names 98 libc and libgcc bodies by matching the Arm GNU
Toolchain 13.2.Rel1 prebuilt archives against the image. This asks the stronger
question the relink needs answered: does a newlib built from source
(abi/refbuild.sh) reproduce those bodies byte for byte where they sit?

A body is compared with every field a relocation owns masked out -- the
displacement of a call, the word of a literal pool -- because those are the only
bytes the linker writes and they cannot agree before a link. Everything else is
the compiler's output and has to agree exactly or the configuration is wrong.
That is what decides configure options the archives cannot: the image's
__sseek stores FILE->_offset at +0x50, which is the full struct _reent layout,
so _REENT_SMALL is off, and with it off five stdio bodies that differed agree.
"""

import argparse
import glob
import os
import subprocess
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
APP_BASE = 0x27000


class Archive(object):
    """Every function body an archive defines, by symbol name."""

    def __init__(self, tools, scratch):
        self.tools, self.scratch, self.bodies = tools, scratch, {}

    def add(self, path):
        into = os.path.join(self.scratch, os.path.basename(path).replace(".", "_"))
        if not os.path.isdir(into):
            os.makedirs(into)
            subprocess.run([self.tools + "ar", "x", path], cwd=into, check=True)
        for obj in sorted(glob.glob(os.path.join(into, "*.o"))):
            listing = subprocess.run([self.tools + "objdump", "-t", obj],
                                     capture_output=True, text=True).stdout
            for line in listing.splitlines():
                row = line.split()
                sections = [w for w in row if w.startswith(".text")]
                if len(row) < 6 or "F" not in row[2:5] or not sections:
                    continue
                section = sections[0]
                size = int(row[row.index(section) + 1], 16)
                if size and row[-1] not in self.bodies:
                    self.bodies[row[-1]] = (obj, section, int(row[0], 16), size)

    def body(self, name):
        obj, section, off, size = self.bodies[name]
        out = os.path.join(self.scratch, "body.bin")
        subprocess.run([self.tools + "objcopy", "-O", "binary", "--only-section",
                        section, obj, out], check=True)
        with open(out, "rb") as fh:
            return fh.read()[off:off + size], self.relocated(obj, section, off, size)

    def relocated(self, obj, section, off, size):
        """The byte offsets inside the body that a relocation owns."""
        listing = subprocess.run([self.tools + "objdump", "-r", obj],
                                 capture_output=True, text=True).stdout
        owned, current = set(), None
        for line in listing.splitlines():
            if line.startswith("RELOCATION RECORDS FOR ["):
                current = line.split("[")[1].split("]")[0]
                continue
            row = line.split()
            if current != section or len(row) < 2:
                continue
            try:
                at = int(row[0], 16)
            except ValueError:
                continue
            owned.update(range(at - off, at - off + 4))
        return owned


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default="newlib-nano-big",
                    help="the abi/refbuild.sh newlib build to check")
    ap.add_argument("--root", default=os.path.expanduser("~/ref-build"))
    ap.add_argument("--scratch", default="/tmp/libc_check")
    ap.add_argument("--list", type=int, default=20)
    args = ap.parse_args()

    tools = os.path.join(args.root, "tc",
                         "arm-gnu-toolchain-13.2.Rel1-x86_64-arm-none-eabi",
                         "bin", "arm-none-eabi-")
    newlib = os.path.join(args.root, "build", args.build, "arm-none-eabi", "newlib")
    libgcc = os.path.join(args.root, "tc",
                          "arm-gnu-toolchain-13.2.Rel1-x86_64-arm-none-eabi", "lib",
                          "gcc", "arm-none-eabi", "13.2.1", "thumb", "v7e-m+fp",
                          "hard", "libgcc.a")
    archive = Archive(tools, os.path.join(args.scratch, args.build))
    for path in (os.path.join(newlib, "libc.a"), os.path.join(newlib, "libm.a"),
                 libgcc):
        if not os.path.exists(path):
            sys.exit("%s does not exist; run abi/refbuild.sh" % path)
        archive.add(path)

    with open(os.path.join(SIM, "appl.bin"), "rb") as fh:
        image = fh.read()
    names = yaml.safe_load(open(os.path.join(HERE, "autonames.yaml")))
    wanted = sorted(((f["address"], f["name"]) for f in names["functions"]
                     if f["class"] == "libc"))

    exact, masked, differing, absent = 0, 0, [], []
    for addr, name in wanted:
        if name not in archive.bodies:
            absent.append(name)
            continue
        built, owned = archive.body(name)
        there = image[addr - APP_BASE:addr - APP_BASE + len(built)]
        if built == there:
            exact += 1
            continue
        bad = [i for i in range(len(built))
               if built[i] != there[i] and i not in owned]
        if bad:
            differing.append((name, addr, len(bad), len(built)))
        else:
            masked += 1

    print("%s against appl.bin: %d named libc and libgcc bodies"
          % (args.build, len(wanted)))
    print("  %d reproduce the image's bytes exactly" % exact)
    print("  %d reproduce them once the relocated fields are masked" % masked)
    print("  %d still differ" % len(differing))
    for name, addr, bad, size in differing[:args.list]:
        print("      %-20s 0x%05x  %d of %d bytes" % (name, addr, bad, size))
    print("  %d have no symbol in the build: %s" % (len(absent), ", ".join(absent)))
    return 1 if differing else 0


if __name__ == "__main__":
    sys.exit(main())
