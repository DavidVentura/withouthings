#!/bin/bash
# Prove the objectified app links back to the stock image byte for byte.
#
#   abi/identity.sh
#   REPLACE_ARGS="--replace <group>" abi/identity.sh
#
# A replacement is byte-identical too: its stand-in definition is the original
# body's own address, so the call sites relocate back onto the bytes they held.
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

python3 blobify.py -o "$OUT/appl-blob.o" ${REPLACE_ARGS:-}
"$GCC-ld" -L ../out -T identity.ld --emit-relocs -e 0 \
    -o "$OUT/identity.elf" "$OUT/appl-blob.o" "$OUT/stock-defs.o"
"$GCC-objcopy" -O binary --only-section=.blob "$OUT/identity.elf" "$OUT/identity.bin"

python3 - "$OUT/identity.bin" ../flash.bin "$OUT/identity.elf" "$OUT/appl-blob.o" <<'PY'
import struct
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


def relocations(path, kind):
    """Every R_ARM_ABS32 site of an ELF32-LE file, from its REL sections."""
    data = open(path, "rb").read()
    shoff, = struct.unpack_from("<I", data, 0x20)
    entsize, num = struct.unpack_from("<HH", data, 0x2E)
    sites = []
    for i in range(num):
        head = shoff + i * entsize
        styp, _, _, off, size, _, _, _, ent = struct.unpack_from("<9I", data, head + 4)
        if styp != 9 or not ent:
            continue
        for at in range(off, off + size, ent):
            r_offset, r_info = struct.unpack_from("<II", data, at)
            if r_info & 0xFF == 2:
                sites.append(r_offset)
    return sites


# The bytes agreeing is not by itself proof that the words the object declares
# as relocations are the words the linker wrote: a relocation the linker dropped
# would leave the blanked word behind, and a word blobify never blanked would
# agree for the wrong reason. --emit-relocs hands back what was really applied.
emitted = [a for a in relocations(sys.argv[3], "linked") if 0x27000 <= a < 0xf117c]
declared = relocations(sys.argv[4], "object")
if len(emitted) != len(declared):
    sys.exit("the object declares %d absolute relocations, the link emitted %d"
             % (len(declared), len(emitted)))
bad = [a for a in emitted
       if linked[a - 0x27000:a - 0x27000 + 4] != stock[a - 0x27000:a - 0x27000 + 4]]
if bad:
    sys.exit("%d absolute relocations did not resolve to the original word"
             % len(bad))
print("%d absolute relocations resolve to the word they replaced" % len(emitted))
PY
