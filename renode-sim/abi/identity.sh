#!/bin/bash
# Prove the objectified app links back to the stock image byte for byte.
#
#   abi/identity.sh
#
# abi/blobify.py cuts the image into one section per function and per data item
# and turns every internal branch that leaves a section into a relocation; this
# links those sections at their original addresses (out/relink-place.ld) with
# the library symbols bound back to the blob's own copy (stock-defs.o)
# and nothing else. Any difference from flash.bin's app slice is a bug in the
# cutting or in a relocation, and this is the only check that sees it before
# the sim does.
set -eu
cd "$(dirname "$0")"
ROOT=${ROOT:-$HOME/ref-build}
GCC=$ROOT/gcc-arm-none-eabi-9-2020-q2-update/bin/arm-none-eabi
OUT=../out/relink
mkdir -p "$OUT"

python3 blobify.py -o "$OUT/appl-blob.o"
"$GCC-ld" -L ../out -T identity.ld --emit-relocs -e 0 \
    -o "$OUT/identity.elf" "$OUT/appl-blob.o" "$OUT/stock-defs.o"
"$GCC-objcopy" -O binary --only-section=.blob "$OUT/identity.elf" "$OUT/identity.bin"

python3 - "$OUT/identity.bin" ../flash.bin <<'PY'
import sys
linked = open(sys.argv[1], "rb").read()
stock = open(sys.argv[2], "rb").read()[0x27000:0xf117c]
if len(linked) != len(stock):
    sys.exit("the link is %d bytes, the app image is %d" % (len(linked), len(stock)))
bad = [i for i in range(len(stock)) if linked[i] != stock[i]]
if bad:
    for i in bad[:20]:
        print("0x%x: linked %02x, stock %02x" % (0x27000 + i, linked[i], stock[i]))
    sys.exit("%d bytes differ from the stock app image" % len(bad))
print("byte-identical: %d bytes at 0x27000..0xf117c" % len(stock))
PY
