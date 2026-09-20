#!/usr/bin/env python3
"""Score every nrfx driver of every nrfx release against the image, by driver.

    python3 abi/nrfx_survey.py                 # the releases abi/refbuild.sh builds
    python3 abi/nrfx_survey.py --releases 2.1.0,2.2.0
    python3 abi/nrfx_survey.py --top 5         # more than the best body per driver

The SAADC driver turned out to be nrfx 2.1.0 with a Withings patch, so the
standing question for every other peripheral under the sensors was which nrfx
release, config and patch its driver is. This answers it the same way, for all
of them at once: abi/refbuild.sh's `nrfx` recipe builds each release's drivers
standalone with the image's own compiler and flags, abi/match.py's token
matcher proposes where in the image each body could be, and abi/body_check.py
settles the proposal byte for byte.

A driver whose best body anywhere in the 800 KB image is a token alignment in
the twenties or forties, over nine releases, is not that driver. The number is
the evidence for saying so, which is why this prints it rather than a verdict.

The report is by driver and not by symbol because a driver is the unit a
verdict is about: nrfx_twi.c either is or is not what the image's I2C bus
driver was built from, and the body that scores best inside it is the strongest
case the release can make.
"""

import argparse
import collections
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import body_check
import match

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
# The toolchain abi/refbuild.sh builds with; body_check reads its ELFs with the
# matching binutils, which are not on PATH.
TOOLS = os.path.join(os.environ.get("ROOT", os.path.expanduser("~/ref-build")),
                     "tc", "arm-gnu-toolchain-13.2.Rel1-x86_64-arm-none-eabi",
                     "bin", "arm-none-eabi-")
RELEASES = ["1.7.2", "1.8.6", "2.0.0", "2.1.0", "2.2.0", "2.3.0", "2.4.0",
            "2.5.0", "2.6.0"]


def driver_of(variant_dir):
    """symbol -> driver, from the per-driver objects the `nrfx` recipe keeps."""
    owner = {}
    for obj in sorted(os.listdir(variant_dir)):
        if not obj.endswith(".o"):
            continue
        out = subprocess.run([TOOLS + "nm", "--defined-only",
                              os.path.join(variant_dir, obj)],
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            row = line.split()
            if len(row) == 3 and row[1] in "tT":
                owner[row[2]] = obj[:-2]
    return owner


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default=os.path.join(SIM, "appl.bin"))
    ap.add_argument("--releases", default=",".join(RELEASES))
    ap.add_argument("--top", type=int, default=1,
                    help="bodies reported per driver")
    ap.add_argument("--min-insns", type=int, default=4)
    args = ap.parse_args()

    streams = match.parse_image(args.image, match.APP_BASE)
    index = match.build_kgram_index(streams)

    best = collections.defaultdict(dict)   # driver -> symbol -> record
    for release in args.releases.split(","):
        variant = os.path.join(match.REF_ROOT, "nrfx-" + release)
        elf = os.path.join(variant, "ref.elf")
        if not os.path.exists(elf):
            sys.exit("no %s (run ONLY=nrfx abi/refbuild.sh)" % elf)
        owner = driver_of(variant)
        for name, fn in match.parse_reference(elf).items():
            if len(fn["tokens"]) < args.min_insns:
                continue
            driver = owner.get(name, "?")
            for cand in match.candidates(fn, streams, index):
                scored = match.score(fn, streams, cand)
                if scored is None:
                    continue
                seen = best[driver].get(name)
                if seen is None or scored["score"] > seen["score"]:
                    best[driver][name] = dict(scored, release=release,
                                              elf=elf, size=fn["size"])

    objects = {}
    print("%-16s %-34s %-9s %-7s %-7s %-6s %s"
          % ("driver", "body", "address", "release", "score", "bytes", "byte verdict"))
    for driver in sorted(best):
        rows = sorted(best[driver].items(), key=lambda kv: -kv[1]["score"])
        for name, rec in rows[:args.top]:
            obj = objects.get(rec["elf"])
            if obj is None:
                obj = objects[rec["elf"]] = body_check.Object(rec["elf"], TOOLS)
            built, owned = obj.body(name)
            with open(args.image, "rb") as fh:
                fh.seek(rec["address"] - match.APP_BASE)
                there = fh.read(len(built))
            bad = sum(1 for i in range(len(built))
                      if built[i] != there[i] and i not in owned)
            verdict = ("exact" if built == there
                       else "masked" if not bad else "%d/%d differ" % (bad, len(built)))
            # Under abi/match.py's shape rule a body this small is a shape
            # rather than a function, and a verdict on one settles nothing.
            shape = " (a shape)" if len(built) < match.SHAPE_BYTES else ""
            print("%-16s %-34s 0x%-7x %-7s %-7.3f %-6d %s%s"
                  % (driver, name, rec["address"], rec["release"],
                     rec["score"], len(built), verdict, shape))
    # A driver with no row at all made no proposal anywhere: no six consecutive
    # normalised instructions of any of its bodies occur anywhere in the image.
    every = set()
    for release in args.releases.split(","):
        every |= set(driver_of(os.path.join(match.REF_ROOT, "nrfx-" + release)).values())
    silent = sorted(every - set(best))
    print("\nno proposal anywhere in the image: " + ", ".join(silent))


if __name__ == "__main__":
    sys.exit(main())
