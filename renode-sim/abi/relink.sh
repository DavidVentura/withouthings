#!/bin/bash
# Build FreeRTOS from source with the image's own config, link it against the
# app blob, and write out/flash-relinked.bin.
#
#   abi/relink.sh            # needs ~/ref-build populated by abi/refbuild.sh
#
# The kernel and the SAADC driver: those are the two open-source pieces the
# boundary (abi/boundary.yaml) crosses into.
set -eu
cd "$(dirname "$0")"
ROOT=${ROOT:-$HOME/ref-build}
SDK=$ROOT/sdk/nRF5_SDK_17.1.0_ddde560
TC=$ROOT/tc/arm-gnu-toolchain-13.2.Rel1-x86_64-arm-none-eabi
GCC=$TC/bin/arm-none-eabi
OUT=../out/relink
mkdir -p "$OUT"

ARCH="-mcpu=cortex-m4 -mthumb -mabi=aapcs -mfpu=fpv4-sp-d16 -mfloat-abi=hard"
COMMON="-Os -ffunction-sections -fdata-sections -fno-builtin
 -fshort-enums -std=gnu99 -g3 -w -fcommon -DNRF52840_XXAA -DFLOAT_ABI_HARD -DS140
 -DSOFTDEVICE_PRESENT -DNRF_SD_BLE_API_VERSION=7 -DFREERTOS -DSWI_DISABLE0"

# abi/stage.sh puts the kernel, the port, Withings' patches and abi/facts.yaml's
# corrections in one tree. abi/refbuild.sh stages the same tree and measures a
# build of it against the image body for body, so what is linked here is what
# the verdicts in abi/matches.yaml were measured on.
./stage.sh "$OUT/src"
KERNEL=$OUT/src/source
PORT=$OUT/src/portable

INC="-Iconfig-relink -I$KERNEL/include -I$PORT/GCC/nrf52 -I$PORT/CMSIS/nrf52 -I$SDK/examples/ble_peripheral/ble_app_hrs_freertos/pca10056/s140/config"
for d in components/toolchain/cmsis/include modules/nrfx modules/nrfx/hal \
         modules/nrfx/mdk modules/nrfx/drivers/include components/libraries/util \
         components/softdevice/s140/headers components/softdevice/s140/headers/nrf52 \
         integration/nrfx components/libraries/experimental_section_vars \
         components/softdevice/common components/libraries/log components/libraries/log/src \
         components/libraries/delay components/libraries/atomic components/libraries/mutex \
         modules/nrfx/soc integration/nrfx/legacy; do
    INC="$INC -I$SDK/$d"
done

objs=""
"$GCC-gcc" $ARCH $COMMON $INC -DconfigUSE_TIMERS=0 -c relink_glue.c -o "$OUT/relink_glue.o"
objs="$objs $OUT/relink_glue.o"
for s in tasks.c queue.c list.c portable/MemMang/heap_4.c; do
    o="$OUT/$(echo "$s" | tr / _ | sed 's/\.c$/.o/')"
    "$GCC-gcc" $ARCH $COMMON $INC -DconfigUSE_TIMERS=0 -c "$KERNEL/$s" -o "$o"
    objs="$objs $o"
done
for s in "$PORT/GCC/nrf52/port.c" "$PORT/CMSIS/nrf52/port_cmsis.c" \
         "$PORT/CMSIS/nrf52/port_cmsis_systick.c"; do
    o="$OUT/$(basename "$s" .c).o"
    "$GCC-gcc" $ARCH $COMMON $INC -DconfigUSE_TIMERS=0 -DREF_ASSERT=0 -c "$s" -o "$o"
    objs="$objs $o"
done

# The SAADC driver. It is nrfx 2.1.0 rather than the SDK's nrfx 1.9.0, with
# abi/patches/nrfx/saadc-withings.patch applied (refbuild.sh stages the tree,
# because the body check that established the patch builds from the same one),
# and the patched tree's headers have to come before the SDK's so the driver and
# the app agree on the 12-byte nrfx_saadc_channel_t. abi/config-relink/nrfx_glue.h
# is already first on INC and supplies the two glue macros the image shows.
# The driver reaches two app symbols, both named in abi/boundary.yaml's
# lib_to_app: memset and withings_irq_priority_set.
NRFX=$ROOT/nrfx/nrfx-2.1.0-withings
if [ ! -d "$NRFX" ]; then
    echo "$NRFX is missing; run abi/refbuild.sh" >&2
    exit 1
fi
# The flags are the driver's own and not $COMMON: -fno-builtin is deliberately
# not passed, because the image's channels_config calls memset for the
# pselp/pseln reset and only the builtin emits that call. abi/refbuild.sh
# compiles the `app` reference the same way, so the object the body verdicts
# were measured on is the object linked here.
NRFX_INC="-I$NRFX -I$NRFX/hal -I$NRFX/drivers -I$NRFX/drivers/include -I$NRFX/soc"
"$GCC-gcc" $ARCH -Os -ffunction-sections -fdata-sections \
    -fshort-enums -std=gnu99 -g3 -w -DNRF52840_XXAA -DFLOAT_ABI_HARD -DS140 \
    -DSOFTDEVICE_PRESENT -DNRF_SD_BLE_API_VERSION=7 -DFREERTOS -DSWI_DISABLE0 \
    -Iconfig-relink $NRFX_INC $INC -DNRFX_SAADC_ENABLED=1 \
    -DSAADC_ENABLED=1 -DNRF_LOG_ENABLED=0 \
    -c "$NRFX/drivers/src/nrfx_saadc.c" -o "$OUT/nrfx_saadc.o"
objs="$objs $OUT/nrfx_saadc.o"

# REPLACE=<group>[,<group>] binds every reference into the sections
# abi/replacements.yaml's group names to the symbol its source defines; a prune
# whose feature names a group turns it on by itself, so the argument is built
# here and handed to every blobify run including the identity one.
REPLACE_ARGS=""
LIBS=""
for g in $(echo "${REPLACE:-}" | tr , ' '); do REPLACE_ARGS="$REPLACE_ARGS --replace $g"; done
# A prune's own replacement groups belong here too. The identity run is what
# writes out/replace.h and out/replace-sources.txt, and the replacement sources
# are compiled from that list before the real cut; a group the identity run did
# not know about is a source nothing compiles and a stub symbol the link cannot
# find. The prune's byte edits stay out of the identity run, because a
# replacement is byte-identical and an edit is not.
for g in $(python3 -c "import blobify, sys; print(\" \".join(blobify.prune_replacements(\"prunes.yaml\", sys.argv[1:])))" ${PRUNE:-}); do
    case " $REPLACE_ARGS " in *" $g "*) ;; *) REPLACE_ARGS="$REPLACE_ARGS --replace $g" ;; esac
done

# SPILL is a list of linker input-file patterns, one --spill each: the flash
# --layout pack frees is inside the app's own span and an archive named here
# goes there instead of into the library region after the image.
SPILL_ARGS=""
for pattern in ${SPILL:-}; do SPILL_ARGS="$SPILL_ARGS --spill $pattern"; done

# The newlib group swaps the image's libc for the source build's, so the link
# needs the archives it swapped them for. abi/libc_check.py's verdicts are what
# the group derives its entries from, so they are measured here rather than
# carried in the repo: a newlib built differently gives different ones.
# libm is a group of its own and an archive of its own: taking the math bodies
# from the source build pulls libm.a's code and tables into the library region,
# so REPLACE=newlib is libc plus libgcc and REPLACE=newlib,libm is both.
# The build replacements.yaml's derive groups are measured against, and so the
# archive the link takes the bodies from: abi/patches/newlib/nano-io-long-long.patch
# is the option that makes the nano printf and scanf honour long long, which is
# how the image was configured.
NEWLIB=${NEWLIB:-newlib-nano-ll}
case ",${REPLACE:-}," in *,newlib,*)
    python3 libc_check.py --build "$NEWLIB" --class libc --emit "$OUT/libc-bodies.yaml"
    LIBS="$LIBS $ROOT/build/$NEWLIB/arm-none-eabi/newlib/libc.a
 $TC/lib/gcc/arm-none-eabi/13.2.1/thumb/v7e-m+fp/hard/libgcc.a"
    ;;
esac
case ",${REPLACE:-}," in *,libm,*)
    python3 libc_check.py --build "$NEWLIB" --class libm --emit "$OUT/libm-bodies.yaml"
    LIBS="$LIBS $ROOT/build/$NEWLIB/arm-none-eabi/newlib/libm.a
 $TC/lib/gcc/arm-none-eabi/13.2.1/thumb/v7e-m+fp/hard/libgcc.a"
    ;;
esac

# The objectified blob must link back to the stock image before it is worth
# linking against anything else; this writes $OUT/appl-blob.o and the two
# generated fragments the real link then reuses.
# DATA=1 links the app's data from the source abi/datagen.py writes rather than
# from the blob object; identity.sh proves that half byte for byte first.
DATA_OBJ=""
DATA_ARGS=""
if [ -n "${DATA:-}" ]; then
    DATA_ARGS="--data-source $(cd ..; pwd)/out/data"
    DATA_OBJ="$OUT/appl-data.o"
else
    # A link without DATA=1 has no data object, and abi/stale_scan.py reads
    # every object of the link to find out where a section used to be, so one
    # left behind by an earlier run would answer for sections this link places
    # itself.
    rm -f "$OUT/appl-data.o"
fi
# Every name the link line already defines is one the partition must be stopped
# from publishing a second definition of: the blob's `__subdf3` section carries
# __aeabi_dadd as an interior label and libgcc's _arm_addsubdf3.o defines it
# too, and the source kernel objects collide the same way -- the blob's copy of
# the RTC2 tick setup at 0x74034 carries the map's name for it and
# port_cmsis_systick.o defines vPortSetupTimerInterrupt as well. That one is
# invisible from abi/boundary.yaml because nothing in Withings code calls it:
# the only caller is vPortStartScheduler, which is library code itself. Both
# cuts of the object -- the identity one and the real one -- get the same
# reservation, so the object the byte-identical link proves is the object the
# real link takes; the --gc branch reserves the same names a second time
# through --also-linked, which is the roots argument and not this one.
RESERVE_ARGS=""
for a in $objs $LIBS; do RESERVE_ARGS="$RESERVE_ARGS --reserve-defs $a"; done

REPLACE_ARGS="$REPLACE_ARGS" RESERVE_ARGS="$RESERVE_ARGS" LINK_ARCHIVES="$LIBS" ./identity.sh

# The replacement sources, compiled against the hand headers the rest of the
# new code uses (abi/include/withings) plus out/replace.h, which the identity
# run above wrote from replacements.yaml so the prototype and the evidence for
# it live together. They are built here rather than after the placement because
# the --gc walk needs every object on the link line: a section whose only
# keeper is a replacement body is kept by the real link and looks dead to a
# walk that enters the app alone.
while read -r src; do
    [ -n "$src" ] || continue
    o="$OUT/replace_$(basename "$src" .c).o"
    "$GCC-gcc" $ARCH $COMMON -I../out -Iinclude -c "$src" -o "$o"
    objs="$objs $o"
done < ../out/replace-sources.txt
# Every object and archive the link will see, so the --gc walk enters the blob
# everywhere the link does: the source kernel, the glue, the replacement bodies
# and the C library, which is the only thing that reaches five of the syscalls
# the image exports.
ALSO_ARGS=""
for o in $objs $LIBS; do ALSO_ARGS="$ALSO_ARGS --also-linked $o"; done
# LAYOUT=shift|reverse re-cuts the same object with every text section moved;
# the identity link above still ran first, so the cutting is proven either way.
# GC=1 re-cuts it with a placement that KEEPs only what the linker cannot see,
# so --gc-sections drops what no root reaches; LAYOUT=reverse then means the
# order the kept sections are packed in, not addresses chosen in advance.
# PRUNE=<feature> applies abi/prunes.yaml's edits before the cut, which only
# means anything together with GC=1: the edits make the feature unreachable and
# --gc-sections is what removes it.
if [ -n "${GC:-}" ]; then
    python3 blobify.py -o "$OUT/appl-blob.o" --gc $ALSO_ARGS $RESERVE_ARGS ${LAYOUT:+--layout "$LAYOUT"} $SPILL_ARGS ${KEEP_ALSO:+--keep-also "$KEEP_ALSO"} ${PRUNE:+--prune "$PRUNE"} $REPLACE_ARGS $DATA_ARGS
elif [ -n "${LAYOUT:-}" ] || [ -n "$REPLACE_ARGS" ] || [ -n "${PRUNE:-}" ] || [ -n "${DATA:-}" ]; then
    python3 blobify.py -o "$OUT/appl-blob.o" ${LAYOUT:+--layout "$LAYOUT"} $SPILL_ARGS ${PRUNE:+--prune "$PRUNE"} $REPLACE_ARGS $RESERVE_ARGS $DATA_ARGS
fi
if [ -n "${DATA:-}" ]; then ./datagen.sh; fi

# The map and the list of what gc removed are the inputs abi/stale_scan.py needs
# to check a gc link: where each surviving section ended up, and which ranges
# are gone, so a word still holding one of those addresses can be found.
"$GCC-ld" -L ../out -T relink.ld --gc-sections --print-gc-sections -M \
    --emit-relocs -o "$OUT/relinked.elf" "$OUT/appl-blob.o" $DATA_OBJ $objs $LIBS \
    > "$OUT/relinked.map" 2> "$OUT/relinked.gc"
cat "$OUT/relinked.gc" >&2
"$GCC-objcopy" -O binary --gap-fill 0xff "$OUT/relinked.elf" "$OUT/appl.bin"
"$GCC-size" -A "$OUT/relinked.elf"
python3 relink_image.py
