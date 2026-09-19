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
GCC=$ROOT/tc/arm-gnu-toolchain-13.2.Rel1-x86_64-arm-none-eabi/bin/arm-none-eabi
OUT=../out/relink
mkdir -p "$OUT"

# DATA=1 takes the app's data out of the object as well and links it from the
# source abi/datagen.py writes: the same sections, the same relocations, the
# same symbols, expressed as assembler and as typed C initialisers. The
# byte-identical link is what says the source reproduces the image's data.
DATA_OBJ=""
if [ -n "${DATA:-}" ]; then
    DATA_ARGS="--data-source $(cd ..; pwd)/out/data"
    DATA_OBJ="$OUT/appl-data.o"
fi
python3 blobify.py -o "$OUT/appl-blob.o" ${REPLACE_ARGS:-} ${RESERVE_ARGS:-} ${DATA_ARGS:-}
if [ -n "${DATA:-}" ]; then ./datagen.sh; fi
"$GCC-ld" -L ../out -T identity.ld --emit-relocs -e 0 \
    -o "$OUT/identity.elf" "$OUT/appl-blob.o" $DATA_OBJ "$OUT/stock-defs.o"
# The three output sections the image is made of, in load order: the flash
# below the .data initialiser image, the image itself (whose VMA is RAM and
# whose LMA is where the copy loop reads it), and the flash above it. objcopy
# lays a binary out by load address, so naming all three reproduces the app
# slice exactly; the .bss sections are NOLOAD and contribute nothing.
"$GCC-objcopy" -O binary --only-section=.blob --only-section=.appdata \
    --only-section=.blobtail "$OUT/identity.elf" "$OUT/identity.bin"

python3 - "$OUT/identity.bin" ../flash.bin "$OUT/identity.elf" "$OUT/appl-blob.o" $DATA_OBJ <<'PY'
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
    """Every word-sized relocation site of an ELF32-LE file, from its REL sections.

    R_ARM_ABS32 is an address and R_ARM_REL32 a distance, and both replace a
    word the image already held, so both are checked the same way.
    """
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
            if r_info & 0xFF in (2, 3):
                sites.append(r_offset)
    return sites


def symbols(path):
    """The ELF's symbol table as name -> value."""
    data = open(path, "rb").read()
    shoff, = struct.unpack_from("<I", data, 0x20)
    entsize, num = struct.unpack_from("<HH", data, 0x2E)
    heads = [struct.unpack_from("<10I", data, shoff + i * entsize)
             for i in range(num)]
    out = {}
    for h in heads:
        if h[1] != 2:               # SHT_SYMTAB
            continue
        names = heads[h[6]]
        for at in range(h[4], h[4] + h[5], h[9]):
            n, value = struct.unpack_from("<II", data, at)
            end = data.index(b"\0", names[4] + n)
            out[data[names[4] + n:end].decode()] = value
    return out


# The initialiser image has two addresses: a relocation inside it is recorded at
# the RAM address the run has at run time, and the byte it wrote is at the flash
# address the linker loaded it from. Every other relocation is in flash already.
sym = symbols(sys.argv[3])
DATA_VMA, DATA_END = sym["__data_start__"], sym["__data_end__"]
DATA_LMA = sym["__data_load__"]


def in_image(a):
    """`a` as an offset into the app image, or None if it is not in it."""
    if 0x27000 <= a < 0xf117c:
        return a - 0x27000
    if DATA_VMA <= a < DATA_END:
        return DATA_LMA + (a - DATA_VMA) - 0x27000
    return None


# The bytes agreeing is not by itself proof that the words the object declares
# as relocations are the words the linker wrote: a relocation the linker dropped
# would leave the blanked word behind, and a word blobify never blanked would
# agree for the wrong reason. --emit-relocs hands back what was really applied.
emitted = [a for a in relocations(sys.argv[3], "linked") if in_image(a) is not None]
declared = [a for path in sys.argv[4:] for a in relocations(path, "object")]
if len(emitted) != len(declared):
    sys.exit("the objects declare %d word relocations, the link emitted %d"
             % (len(declared), len(emitted)))
bad = [a for a in emitted
       if linked[in_image(a):in_image(a) + 4] != stock[in_image(a):in_image(a) + 4]]
if bad:
    sys.exit("%d word relocations did not resolve to the original word"
             % len(bad))
print("%d word relocations resolve to the word they replaced" % len(emitted))
PY

# The reservation checked rather than assumed: LINK_ARCHIVES is the archives the
# real link adds behind this one, and a name both they and the partition define
# is a link error waiting for the member to be pulled in for something else.
if [ -n "${LINK_ARCHIVES:-}" ]; then
    ARCH_ARGS=""
    for a in $LINK_ARCHIVES; do ARCH_ARGS="$ARCH_ARGS --archive $a"; done
    python3 link_defs.py --tools "$GCC-" $ARCH_ARGS "$OUT/appl-blob.o" $DATA_OBJ
fi
