#!/usr/bin/env python3
"""Check a newlib build against the library bodies the image carries.

    python3 abi/libc_check.py [--build newlib-nano-big] [--class libc|libm]

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

--emit writes the same verdicts as a file, which is what abi/replacements.yaml's
`newlib` group derives its entries from: a body the build reproduces is one the
link may take from the archive instead of the blob, and a body it does not is
one the blob has to keep. Deriving them rather than listing them is the point --
the list is 95 names long and is a measurement, not a decision.
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
                    self.bodies[row[-1]] = (obj, section, int(row[0], 16), size,
                                            row[1])

    def body(self, name):
        obj, section, off, size, _ = self.bodies[name]
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
    ap.add_argument("--class", dest="cls", default="libc",
                    help="the abi/autonames.yaml class whose bodies to check;"
                         " libc is the C library and libgcc, libm the math one,"
                         " and they are separate because a link may take one"
                         " without the other")
    ap.add_argument("--root", default=os.path.expanduser("~/ref-build"))
    ap.add_argument("--scratch", default="/tmp/libc_check")
    ap.add_argument("--list", type=int, default=20)
    ap.add_argument("--emit", help="write the verdict for every body as YAML,"
                    " for abi/replacements.yaml's `newlib` group to derive from")
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
                     if f["class"] == args.cls))

    exact, masked, differing, absent, local, verdicts = 0, 0, [], [], [], []
    for addr, name in wanted:
        if name not in archive.bodies:
            absent.append(name)
            verdicts.append((addr, name, "absent", "no symbol in the build"))
            continue
        built, owned = archive.body(name)
        there = image[addr - APP_BASE:addr - APP_BASE + len(built)]
        if archive.bodies[name][4] == "l":
            # A static: the body is there and may well be the image's, but the
            # name is the source file's private one and no link can bind to it.
            local.append(name)
            verdicts.append((addr, name, "local",
                             "the archive defines it local to its object"))
            continue
        if built == there:
            exact += 1
            verdicts.append((addr, name, "exact", "%d bytes" % len(built)))
            continue
        bad = [i for i in range(len(built))
               if built[i] != there[i] and i not in owned]
        if bad:
            differing.append((name, addr, len(bad), len(built)))
            verdicts.append((addr, name, "differs",
                             "%d of %d bytes outside the relocated fields"
                             % (len(bad), len(built))))
        else:
            masked += 1
            verdicts.append((addr, name, "masked",
                             "%d bytes, the relocated fields masked" % len(built)))

    if args.emit:
        os.makedirs(os.path.dirname(os.path.abspath(args.emit)), exist_ok=True)
        with open(args.emit, "w") as fh:
            fh.write("# Generated by abi/libc_check.py, do not edit: whether a\n"
                     "# source build reproduces each libc body the image carries.\n")
            fh.write("build: %s\nclass: %s\n" % (args.build, args.cls))
            fh.write("bodies:\n")
            for addr, name, verdict, why in verdicts:
                fh.write("  - address: 0x%x\n    symbol: %s\n    verdict: %s\n"
                         "    why: %s\n" % (addr, name, verdict, why))

    print("%s against appl.bin: %d bodies abi/autonames.yaml classes %s"
          % (args.build, len(wanted), args.cls))
    print("  %d reproduce the image's bytes exactly" % exact)
    print("  %d reproduce them once the relocated fields are masked" % masked)
    print("  %d still differ" % len(differing))
    for name, addr, bad, size in differing[:args.list]:
        print("      %-20s 0x%05x  %d of %d bytes" % (name, addr, bad, size))
    print("  %d have no symbol in the build: %s" % (len(absent), ", ".join(absent)))
    print("  %d are static in the build, so nothing can link against them: %s"
          % (len(local), ", ".join(sorted(local))))
    # --emit is the generator, and a body that differs is one of its answers;
    # only the standalone check treats it as a failure.
    return 1 if differing and not args.emit else 0


if __name__ == "__main__":
    sys.exit(main())
