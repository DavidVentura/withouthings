#!/usr/bin/env python3
"""Apply abi/facts.yaml's reference-build corrections to a staged header.

    python3 abi/refbuild_fix.py --port <SDK>/external/freertos/portable --out DIR

The reference build is the SDK's, and the SDK is right about the SDK: where it
disagrees with this image it is because this image was configured differently,
and abi/facts.yaml is where that difference and the evidence for it live. The
corrected header goes to DIR and abi/relink.sh shadows the original with it on
the include path, because the macro it changes is an unconditional #define and
there is nothing to -D.
"""

import argparse
import os
import re

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True, help="the SDK's freertos/portable")
    ap.add_argument("--out", required=True, help="where the corrected header goes")
    ap.add_argument("--facts", default=os.path.join(HERE, "facts.yaml"))
    args = ap.parse_args()

    corrections = yaml.safe_load(open(args.facts))["reference_build"]
    for name, fix in sorted(corrections.items()):
        source = os.path.join(args.port, fix["header"])
        text = open(source).read()
        for old, new in fix["replace"]:
            text, count = re.subn(r"\b%s\b" % re.escape(old), new, text)
            if not count:
                raise SystemExit("abi/refbuild_fix.py: %s says %s appears in %s,"
                                 " and it does not" % (name, old, fix["header"]))
        out = os.path.join(args.out, os.path.basename(fix["header"]))
        with open(out, "w") as fh:
            fh.write(text)
        print(out)


if __name__ == "__main__":
    main()
