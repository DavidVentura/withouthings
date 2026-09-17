#!/bin/bash
# Build FreeRTOS from source with the image's own config, link it against the
# app blob, and write out/flash-relinked.bin.
#
#   abi/relink.sh            # needs ~/ref-build populated by abi/refbuild.sh
#
# Only the kernel is built: the boundary (abi/boundary.yaml) crosses into nrfx
# exactly once, the SAADC, and that driver is Withings-modified and kept.
set -eu
cd "$(dirname "$0")"
ROOT=${ROOT:-$HOME/ref-build}
SDK=$ROOT/sdk/nRF5_SDK_17.1.0_ddde560
GCC=$ROOT/gcc-arm-none-eabi-9-2020-q2-update/bin/arm-none-eabi
KERNEL=$SDK/external/freertos/source
PORT=$SDK/external/freertos/portable
OUT=../out/relink
mkdir -p "$OUT"

ARCH="-mcpu=cortex-m4 -mthumb -mabi=aapcs -mfpu=fpv4-sp-d16 -mfloat-abi=hard"
COMMON="-Os -ffunction-sections -fdata-sections -fno-strict-aliasing -fno-builtin
 -fshort-enums -std=gnu99 -g3 -w -DNRF52840_XXAA -DFLOAT_ABI_HARD -DS140
 -DSOFTDEVICE_PRESENT -DNRF_SD_BLE_API_VERSION=7 -DFREERTOS -DSWI_DISABLE0"

# The SDK port hardcodes RTC1 as the tick source; this firmware's tick is RTC2
# (symbols.txt 0x74034 writes RTC2 PRESCALER 0x20, and vector 52 is the tick
# ISR). The macro is an unconditional #define, so the header is rewritten into
# the build directory and shadowed on the include path rather than -D'd.
sed -e 's/NRF_RTC1/NRF_RTC2/' -e 's/RTC1_IRQn/RTC2_IRQn/' \
    "$PORT/CMSIS/nrf52/portmacro_cmsis.h" > "$OUT/portmacro_cmsis.h"

# The kernel is compiled out of a staged copy so abi/patches/ can recover the
# changes Withings made to it; each patch is -p1 against the kernel source dir.
SRC=$OUT/src
rm -rf "$SRC"
cp -r "$KERNEL" "$SRC"
for p in patches/*.patch; do
    patch -s -d "$SRC" -p1 < "$p"
done
KERNEL=$SRC

INC="-I$OUT -Iconfig-relink -I$KERNEL/include -I$PORT/GCC/nrf52 -I$PORT/CMSIS/nrf52 -I$SDK/examples/ble_peripheral/ble_app_hrs_freertos/pca10056/s140/config"
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
# port.c's configASSERTs are build-time sanity checks about the core revision
# (it compares CPUID against 0x410fc241) and the image does not carry them:
# xPortStartScheduler in the blob has no such compare. Building them in wedges
# the boot on Renode's CPUID, so port.c takes the empty assert.
for s in "$PORT/GCC/nrf52/port.c" "$PORT/CMSIS/nrf52/port_cmsis.c" \
         "$PORT/CMSIS/nrf52/port_cmsis_systick.c"; do
    o="$OUT/$(basename "$s" .c).o"
    "$GCC-gcc" $ARCH $COMMON $INC -DconfigUSE_TIMERS=0 -DREF_ASSERT=2 -c "$s" -o "$o"
    objs="$objs $o"
done

# The objectified blob must link back to the stock image before it is worth
# linking against anything else; this writes $OUT/appl-blob.o and the two
# generated fragments the real link then reuses.
./identity.sh
# LAYOUT=shift|reverse re-cuts the same object with every text section moved;
# the identity link above still ran first, so the cutting is proven either way.
# GC=1 re-cuts it with a placement that KEEPs only what the linker cannot see,
# so --gc-sections drops what no root reaches; LAYOUT=reverse then means the
# order the kept sections are packed in, not addresses chosen in advance.
# PRUNE=<feature> applies abi/prunes.yaml's edits before the cut, which only
# means anything together with GC=1: the edits make the feature unreachable and
# --gc-sections is what removes it.
if [ -n "${GC:-}" ]; then
    python3 blobify.py -o "$OUT/appl-blob.o" --gc ${LAYOUT:+--layout "$LAYOUT"} ${KEEP_ALSO:+--keep-also "$KEEP_ALSO"} ${PRUNE:+--prune "$PRUNE"}
elif [ -n "${LAYOUT:-}" ]; then
    python3 blobify.py -o "$OUT/appl-blob.o" --layout "$LAYOUT"
fi
# The map and the list of what gc removed are the inputs abi/stale_scan.py needs
# to check a gc link: where each surviving section ended up, and which ranges
# are gone, so a word still holding one of those addresses can be found.
"$GCC-ld" -L ../out -T relink.ld --gc-sections --print-gc-sections -M \
    --emit-relocs -o "$OUT/relinked.elf" "$OUT/appl-blob.o" $objs \
    > "$OUT/relinked.map" 2> "$OUT/relinked.gc"
cat "$OUT/relinked.gc" >&2
"$GCC-objcopy" -O binary --gap-fill 0xff "$OUT/relinked.elf" "$OUT/appl.bin"
"$GCC-size" -A "$OUT/relinked.elf"
python3 relink_image.py
