#!/bin/bash
# Assemble and compile what abi/datagen.py wrote, into one relocatable.
#
#   abi/datagen.sh
#
# abi/blobify.py --data-source writes out/data: one assembler file per data
# section with the bytes, the labels and the relocations, `all.s` including them
# in address order, and a C file per typed table. They are folded into a single
# object because the placement fragment names the object every section comes
# from, and one name is the whole difference.
set -eu
cd "$(dirname "$0")"
ROOT=${ROOT:-$HOME/ref-build}
GCC=$ROOT/gcc-arm-none-eabi-9-2020-q2-update/bin/arm-none-eabi
OUT=../out/relink
DATA=../out/data
ARCH="-mcpu=cortex-m4 -mthumb -mabi=aapcs -mfpu=fpv4-sp-d16 -mfloat-abi=hard"

objs="$OUT/data-bytes.o"
"$GCC-as" -mcpu=cortex-m4 -mthumb -I "$DATA" -o "$OUT/data-bytes.o" "$DATA/all.s"
while read -r src; do
    [ -n "$src" ] || continue
    o="$OUT/data_$(basename "$src" .c).o"
    "$GCC-gcc" $ARCH -Os -ffunction-sections -fdata-sections -fshort-enums \
        -std=gnu99 -w -I ../out -c "$src" -o "$o"
    objs="$objs $o"
done < ../out/data-sources.txt
"$GCC-ld" -r -o "$OUT/appl-data.o" $objs
