#!/bin/bash
# Move every text section and look for a reference that did not follow it.
#
#   abi/move_scan.sh shift|reverse
#
# The validation DEVELOPMENT.md's plan describes: link with a placement in which
# no function keeps its address, then scan the result for a 32-bit word that
# still holds an old one. The link is the identity link -- the blob against its
# own copy of the library -- because the point is the app's own references, not
# the boundary's. abi/stale_scan.py joins every survivor to what
# abi/classify_words.py decided it was, so a constant that collided with a moved
# range explains itself and anything else is a missed reference.
set -eu
cd "$(dirname "$0")"
LAYOUT=${1:?usage: move_scan.sh shift|reverse}
ROOT=${ROOT:-$HOME/ref-build}
GCC=$ROOT/gcc-arm-none-eabi-9-2020-q2-update/bin/arm-none-eabi
OUT=../out/relink
mkdir -p "$OUT"

python3 blobify.py -o "$OUT/appl-blob.o" --layout "$LAYOUT"
"$GCC-ld" -L ../out -T identity.ld --emit-relocs -e 0 \
    -o "$OUT/moved.elf" "$OUT/appl-blob.o" "$OUT/stock-defs.o"
python3 stale_scan.py --elf "$OUT/moved.elf"
