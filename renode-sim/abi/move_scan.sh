#!/bin/bash
# Move every section and look for a reference that did not follow it.
#
#   DATA=1 abi/move_scan.sh shift|reverse|pack
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
LAYOUT=${1:?usage: move_scan.sh shift|reverse|pack}
ROOT=${ROOT:-$HOME/ref-build}
GCC=$ROOT/gcc-arm-none-eabi-9-2020-q2-update/bin/arm-none-eabi
OUT=../out/relink
mkdir -p "$OUT"

# DATA=1 links the app's data from the source abi/datagen.py writes, which is
# what lets a layout move it; blobify refuses a layout without it. `pack` here
# is the identity link's pack -- nothing is replaced and nothing is dropped, so
# the whole image closes up against the held sections and the slack it squeezes
# out becomes the hole at the top.
DATA_OBJ=""
DATA_ARGS=""
if [ -n "${DATA:-}" ]; then
    DATA_ARGS="--data-source $(cd ..; pwd)/out/data"
    DATA_OBJ="$OUT/appl-data.o"
    # The typed tables include out/hwa10.h and are compiled against it, so it is
    # generated from the manifest here as identity.sh does it; without this the
    # scan compiles this run's tables against the last run's header.
    python3 gen.py --out ../out > /dev/null
fi
python3 blobify.py -o "$OUT/appl-blob.o" --layout "$LAYOUT" $DATA_ARGS
if [ -n "${DATA:-}" ]; then ./datagen.sh; fi
"$GCC-ld" -L ../out -T identity.ld --emit-relocs -e 0 \
    -o "$OUT/moved.elf" "$OUT/appl-blob.o" $DATA_OBJ "$OUT/stock-defs.o"
python3 stale_scan.py --elf "$OUT/moved.elf"
