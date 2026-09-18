#!/bin/bash
# Build the reference objects the image's open-source parts are measured
# against, so abi/match.py can map image addresses to source symbols and
# abi/body_check.py can settle each body byte for byte.
#
#   abi/refbuild.sh          # downloads (once) into ~/ref-build, builds both recipes
#   abi/refbuild.sh --check  # report the newlib archives against abi/newlib-sizes.txt
#
# Two recipes, because the image has two builds in it:
#
#   app        the firmware's own: the SDK's FreeRTOS and nrfx 2.1.0 staged and
#              patched by abi/stage.sh, abi/config-relink/ for the config and the
#              nrfx glue, GCC 13.2 at -Os. This is what abi/relink.sh links.
#   toolchain  newlib 4.3.0.20230120 configured nano with long long, and its
#              libm, built by the same GCC 13.2; libgcc comes from the release.
#
# Nothing here lands in the repo: sources, toolchain and ELFs all live in
# $ROOT (~/ref-build).
#
# Sources, exact versions:
#   nRF5 SDK 17.1.0 (ddde560)
#     https://files.nordicsemi.com/artifactory/nRF5-SDK/external/nRF5_SDK_v17.x.x/nRF5_SDK_17.1.0_ddde560.zip
#     sha256 5bfe38e744c39fd7f30e10077ba12df306ef91f368894795d6a3e7a62dc68061
#     ships FreeRTOS V9.0.0 (external/freertos), nrfx 2.x (modules/nrfx) and the
#     S140 *7.2.0* headers. The image runs S140 7.3.0; 7.2.0 -> 7.3.0 changed no
#     header the reference code compiles against, and no SoftDevice code is built
#     here anyway (it is binary-only), so the headers are only used for nrf_nvic.h
#     and the SVC prototypes.
#   Arm GNU Toolchain 13.2.Rel1 (gcc 13.2.1), for everything. The image's newlib
#     and SAADC driver pinned it first; the kernel then reproduced under it and
#     under nothing else tried. Measured over the 59 kernel, port and driver
#     bodies abi/matches.yaml carried at the time, at -Os with the config below:
#     13.2 and 13.3 agree body for body (30 reproduce), 12.3 loses one and 14.2
#     loses three, and -O2 and -O3 inline half the bodies out of existence
#     before they can be compared at all. 13.3's newlib is 4.4.0, which the
#     image's own assert paths rule out, so 13.2 is the one.
#     The SDK 17.1.0 armgcc makefiles name GCC 9 and every body in the image
#     disagrees with it.
#     https://developer.arm.com/-/media/Files/downloads/gnu/13.2.rel1/binrel/arm-gnu-toolchain-13.2.rel1-x86_64-arm-none-eabi.tar.xz
#
# The config is abi/config-relink/FreeRTOSConfig.h; every value in it that is not
# the SDK's default is forced by something measured in the image.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=${ROOT:-$HOME/ref-build}
SDK=$ROOT/sdk/nRF5_SDK_17.1.0_ddde560
TC=$ROOT/tc/arm-gnu-toolchain-13.2.Rel1-x86_64-arm-none-eabi
GCC=$TC/bin/arm-none-eabi
OUT=$ROOT/build
CFG=$ROOT/cfg

SDK_URL=https://files.nordicsemi.com/artifactory/nRF5-SDK/external/nRF5_SDK_v17.x.x/nRF5_SDK_17.1.0_ddde560.zip
TC_URL=https://developer.arm.com/-/media/Files/downloads/gnu/13.2.rel1/binrel/arm-gnu-toolchain-13.2.rel1-x86_64-arm-none-eabi.tar.xz

mkdir -p "$ROOT/dl" "$ROOT/tc"
if [ ! -d "$SDK" ]; then
    [ -f "$ROOT/dl/sdk.zip" ] || curl -L -o "$ROOT/dl/sdk.zip" "$SDK_URL"
    mkdir -p "$ROOT/sdk" && unzip -q "$ROOT/dl/sdk.zip" -d "$ROOT/sdk"
fi
if [ ! -d "$TC" ]; then
    f="$ROOT/dl/arm-gnu-toolchain-13.2.rel1.tar.xz"
    [ -f "$f" ] || curl -L -o "$f" "$TC_URL"
    tar xf "$f" -C "$ROOT/tc"
fi

mkdir -p "$OUT" "$CFG"
# The config lives in abi/config-relink/ now; a copy left here would shadow it.
rm -f "$CFG/FreeRTOSConfig.h"

# ONLY="<name> <name>" builds just those; everything else is skipped. A build
# directory is the unit of measurement, so rebuilding one is enough.
want() {
    [ -n "${ONLY:-}" ] || return 0
    case " $ONLY " in *" $1 "*) return 0 ;; *) return 1 ;; esac
}

# ---- the staged FreeRTOS ----------------------------------------------------
# abi/stage.sh puts the kernel, the port, Withings' patches and abi/facts.yaml's
# corrections in one tree; abi/relink.sh stages the same way, so the reference
# and the linked library are the same source.
STAGE=$ROOT/src/freertos-withings
"$HERE/stage.sh" "$STAGE"
KERNEL=$STAGE/source
PORT=$STAGE/portable
# ---- include paths ----------------------------------------------------------
INC="-I$HERE/config-relink -I$CFG"
for d in \
  components/libraries/util components/libraries/experimental_section_vars \
  components/libraries/delay components/libraries/atomic components/libraries/log \
  components/libraries/log/src components/libraries/strerror components/libraries/timer components/libraries/sortlist \
  components/libraries/mutex components/libraries/balloc components/libraries/memobj \
  components/libraries/ringbuf components/libraries/queue components/libraries/fifo \
  components/toolchain/cmsis/include \
  components/softdevice/s140/headers components/softdevice/s140/headers/nrf52 \
  components/softdevice/common \
  modules/nrfx modules/nrfx/hal modules/nrfx/mdk modules/nrfx/drivers \
  modules/nrfx/drivers/include modules/nrfx/soc \
  integration/nrfx integration/nrfx/legacy \
  external/fprintf ; do
    INC="$INC -I$SDK/$d"
done
INC="$INC -I$PORT/GCC/nrf52 -I$PORT/CMSIS/nrf52"
INC="$INC -I$SDK/examples/ble_peripheral/ble_app_hrs_freertos/pca10056/s140/config"
# ---- sources ----------------------------------------------------------------
KERNEL_SRC="
tasks.c
queue.c
list.c
timers.c
event_groups.c
stream_buffer.c
croutine.c
portable/MemMang/heap_4.c
"

PORT_SRC="
GCC/nrf52/port.c
CMSIS/nrf52/port_cmsis.c
CMSIS/nrf52/port_cmsis_systick.c
"

SRC="
modules/nrfx/drivers/src/nrfx_spim.c
modules/nrfx/drivers/src/nrfx_spi.c
modules/nrfx/drivers/src/nrfx_twi.c
modules/nrfx/drivers/src/nrfx_twim.c
modules/nrfx/drivers/src/nrfx_twi_twim.c
modules/nrfx/drivers/src/nrfx_saadc.c
modules/nrfx/drivers/src/nrfx_gpiote.c
modules/nrfx/drivers/src/nrfx_nvmc.c
modules/nrfx/drivers/src/nrfx_rtc.c
modules/nrfx/drivers/src/nrfx_timer.c
modules/nrfx/drivers/src/nrfx_ppi.c
modules/nrfx/drivers/src/nrfx_wdt.c
modules/nrfx/drivers/src/nrfx_clock.c
modules/nrfx/drivers/src/nrfx_power.c
modules/nrfx/drivers/src/nrfx_uart.c
modules/nrfx/drivers/src/nrfx_uarte.c
modules/nrfx/drivers/src/nrfx_pwm.c
modules/nrfx/drivers/src/nrfx_swi.c
modules/nrfx/drivers/src/prs/nrfx_prs.c
components/libraries/sortlist/nrf_sortlist.c
modules/nrfx/soc/nrfx_atomic.c
integration/nrfx/legacy/nrf_drv_spi.c
integration/nrfx/legacy/nrf_drv_twi.c
integration/nrfx/legacy/nrf_drv_uart.c
integration/nrfx/legacy/nrf_drv_clock.c
integration/nrfx/legacy/nrf_drv_ppi.c
integration/nrfx/legacy/nrf_drv_power.c
components/libraries/util/app_error.c
components/libraries/util/app_error_weak.c
components/libraries/util/app_error_handler_gcc.c
components/libraries/util/app_util_platform.c
components/libraries/timer/app_timer_freertos.c
components/libraries/atomic/nrf_atomic.c
components/libraries/balloc/nrf_balloc.c
components/libraries/memobj/nrf_memobj.c
components/libraries/ringbuf/nrf_ringbuf.c
components/libraries/queue/nrf_queue.c
components/libraries/fifo/app_fifo.c
components/libraries/strerror/nrf_strerror.c
external/fprintf/nrf_fprintf.c
external/fprintf/nrf_fprintf_format.c
"

# nrf_nvic.h is header-only inline; force it out of line so the critical-region
# helpers the image calls (sd_nvic_critical_region_enter/exit) get real symbols.
cat > "$CFG/nvic_outline.c" <<'EOF'
#include "nrf_nvic.h"
nrf_nvic_state_t nrf_nvic_state;
void ref_assert_log( const char *file, unsigned int line );
uint32_t ref_sd_nvic_critical_region_enter(uint8_t *p) { return sd_nvic_critical_region_enter(p); }
uint32_t ref_sd_nvic_critical_region_exit(uint8_t p)   { return sd_nvic_critical_region_exit(p); }
uint32_t ref_sd_nvic_EnableIRQ(IRQn_Type n)            { return sd_nvic_EnableIRQ(n); }
uint32_t ref_sd_nvic_DisableIRQ(IRQn_Type n)           { return sd_nvic_DisableIRQ(n); }
uint32_t ref_sd_nvic_ClearPendingIRQ(IRQn_Type n)      { return sd_nvic_ClearPendingIRQ(n); }
uint32_t ref_sd_nvic_SetPriority(IRQn_Type n, uint32_t p) { return sd_nvic_SetPriority(n, p); }
EOF

# ---- per-variant defines ----------------------------------------------------
# Drivers the image demonstrably uses (symbols.txt): SPIM1/2/3, TWI0 in legacy
# TWI (not TWIM) mode, blocking SAADC, GPIOTE with the PORT path, RTC1+RTC2.
DEFS="-DNRF52840_XXAA -DBOARD_PCA10056 -DFLOAT_ABI_HARD -DCONFIG_GPIO_AS_PINRESET
 -DS140 -DSOFTDEVICE_PRESENT -DNRF_SD_BLE_API_VERSION=7 -DSWI_DISABLE0
 -DFREERTOS -D__HEAP_SIZE=0 -D__STACK_SIZE=8192
 -DNRFX_SPIM_ENABLED=1 -DNRFX_SPIM0_ENABLED=1 -DNRFX_SPIM1_ENABLED=1
 -DNRFX_SPIM2_ENABLED=1 -DNRFX_SPIM3_ENABLED=1
 -DNRFX_SPI_ENABLED=1 -DNRFX_SPI0_ENABLED=1 -DNRFX_SPI1_ENABLED=1 -DNRFX_SPI2_ENABLED=1
 -DSPI_ENABLED=1 -DSPI0_ENABLED=1 -DSPI1_ENABLED=1 -DSPI2_ENABLED=1
 -DSPI0_USE_EASY_DMA=1 -DSPI1_USE_EASY_DMA=1 -DSPI2_USE_EASY_DMA=1
 -DNRFX_TWI_ENABLED=1 -DNRFX_TWI0_ENABLED=1 -DNRFX_TWI1_ENABLED=1
 -DNRFX_TWIM_ENABLED=1 -DNRFX_TWIM0_ENABLED=1 -DNRFX_TWIM1_ENABLED=1
 -DTWI_ENABLED=1 -DTWI0_ENABLED=1 -DTWI1_ENABLED=1 -DTWI0_USE_EASY_DMA=0
 -DNRFX_SAADC_ENABLED=1 -DSAADC_ENABLED=1
 -DNRFX_GPIOTE_ENABLED=1 -DGPIOTE_ENABLED=1 -DNRFX_GPIOTE_CONFIG_NUM_OF_LOW_POWER_EVENTS=8
 -DGPIOTE_CONFIG_NUM_OF_LOW_POWER_EVENTS=8
 -DNRFX_NVMC_ENABLED=1 -DNRFX_RTC_ENABLED=1 -DNRFX_RTC0_ENABLED=1
 -DNRFX_RTC1_ENABLED=1 -DNRFX_RTC2_ENABLED=0 -DRTC_ENABLED=1
 -DRTC0_ENABLED=1 -DRTC1_ENABLED=1 -DRTC2_ENABLED=0
 -DNRFX_TIMER_ENABLED=1 -DNRFX_TIMER0_ENABLED=1 -DNRFX_TIMER1_ENABLED=1
 -DNRFX_TIMER2_ENABLED=1 -DTIMER_ENABLED=1
 -DTIMER0_ENABLED=1 -DTIMER1_ENABLED=1 -DTIMER2_ENABLED=1
 -DNRFX_PPI_ENABLED=1 -DPPI_ENABLED=1
 -DNRFX_WDT_ENABLED=1 -DWDT_ENABLED=1
 -DNRFX_CLOCK_ENABLED=1 -DCLOCK_ENABLED=1 -DNRFX_POWER_ENABLED=1 -DPOWER_ENABLED=1
 -DNRFX_UART_ENABLED=1 -DNRFX_UART0_ENABLED=1 -DUART_ENABLED=1 -DUART0_ENABLED=1
 -DUART_LEGACY_SUPPORT=1 -DUART_EASY_DMA_SUPPORT=0
 -DNRFX_UARTE_ENABLED=1 -DNRFX_UARTE0_ENABLED=1
 -DNRFX_PWM_ENABLED=1 -DNRFX_PWM0_ENABLED=1 -DPWM_ENABLED=1 -DPWM0_ENABLED=1
 -DNRFX_PRS_ENABLED=1 -DNRFX_PRS_BOX_0_ENABLED=1 -DNRFX_PRS_BOX_1_ENABLED=1
 -DNRFX_PRS_BOX_2_ENABLED=1 -DNRFX_PRS_BOX_3_ENABLED=1 -DNRFX_PRS_BOX_4_ENABLED=1
 -DPERIPHERAL_RESOURCE_SHARING_ENABLED=1 -DNRF_SORTLIST_ENABLED=1
 -DNRFX_SWI_ENABLED=1 -DAPP_TIMER_ENABLED=1 -DAPP_TIMER_V2
 -DNRF_BALLOC_ENABLED=1 -DNRF_MEMOBJ_ENABLED=1 -DNRF_RINGBUF_ENABLED=1
 -DNRF_QUEUE_ENABLED=1 -DNRF_STRERROR_ENABLED=1 -DNRF_FPRINTF_ENABLED=1
 -DAPP_FIFO_ENABLED=1 -DNRF_LOG_ENABLED=0"

ARCH="-mcpu=cortex-m4 -mthumb -mabi=aapcs -mfpu=fpv4-sp-d16 -mfloat-abi=hard"
# No -fno-strict-aliasing: the image's own code is the measurement. With strict
# aliasing off GCC has to reload a field it just stored through another pointer,
# and seven bodies carry that reload where the image does not -- uxListRemove
# (0x9e55a) is the clearest, keeping pxNext and pxPrevious in the registers the
# ldrd loaded them into across both list stores. Turning it on settles all seven.
COMMON="-ffunction-sections -fdata-sections -fno-builtin
 -fshort-enums -std=gnu99 -g3 -w"
build_variant() {
    local name="$1"; shift
    want "$name" || return 0
    local od="$OUT/$name"
    rm -rf "$od"; mkdir -p "$od"
    local objs=""
    local kinc="-I$KERNEL/include"
    for s in $KERNEL_SRC; do
        local o="$od/kernel_$(echo "$s" | tr / _ | sed 's/\.c$/.o/')"
        if "$GCC-gcc" $ARCH $COMMON "$@" $DEFS $kinc $INC -c "$KERNEL/$s" -o "$o" 2>>"$od/err.log"; then
            objs="$objs $o"
        else
            echo "  skip $KERNEL/$s" >> "$od/skipped.log"
        fi
    done
    INC="$kinc $INC"
    for s in $PORT_SRC $SRC; do
        local o="$od/$(echo "$s" | tr / _ | sed 's/\.c$/.o/')"
        case " ${SKIP_SRC:-} " in *" $s "*) continue ;; esac
        local src="$SDK/$s"
        case "$s" in GCC/*|CMSIS/*) src="$PORT/$s" ;; esac
        [ -f "$src" ] || { echo "  skip $src (absent)" >> "$od/skipped.log"; continue; }
        if "$GCC-gcc" $ARCH $COMMON "$@" $DEFS $INC -c "$src" -o "$o" 2>>"$od/err.log"; then
            objs="$objs $o"
        else
            echo "  skip $s" >> "$od/skipped.log"
        fi
    done
    "$GCC-gcc" $ARCH $COMMON "$@" $DEFS $INC -c "$CFG/nvic_outline.c" -o "$od/nvic_outline.o" 2>>"$od/err.log" \
        && objs="$objs $od/nvic_outline.o"
    # Partial link keeps one symbol table without needing a real image layout.
    INC="${INC#$kinc }"
    "$GCC-ld" -r -o "$od/ref.elf" $objs
    printf '%-10s %3d objs  %s\n' "$name" "$(echo $objs | wc -w)" "$od/ref.elf"
}

# ---- newlib from source -----------------------------------------------------
# The image's libc is newlib 4.3.0.20230120 built by Withings themselves: the
# three assert paths it carries begin
# /home/mbouillot/dev/pro/cortex-toolchain/src/newlib-4.3.0.20230120/, so the
# vanilla tarball is the source and the Arm prebuilt archives are only a
# reference. Two configurations are built because one question the image answers
# and the archives do not is whether _REENT_SMALL is on: it is not, and
# abi/libc_check.py is the measurement (__sseek stores FILE->_offset at +0x50,
# which only the full struct gives; reent-small puts it at +0x54).
#
# The compiler is the Arm 13.2.Rel1 one, whose GCC major the image's codegen
# pins, and the multilib flags are the image's own: Cortex-M4, hard float.
NEWLIB=newlib-4.3.0.20230120
NEWLIB_URL=https://sourceware.org/pub/newlib/$NEWLIB.tar.gz
NEWLIB_CC=$ROOT/tc/arm-gnu-toolchain-13.2.Rel1-x86_64-arm-none-eabi/bin
# No -fdata-sections, which Arm's own newlib build does pass. The image's libm
# is the measurement: __ieee754_sqrt (0x8cd6c) reads `one` and `tiny` as one
# object at base and base+8, which only holds while the two sit in a shared
# .rodata; with -fdata-sections each gets a section of its own, the body needs a
# second pool word and r11 with it, and the register save mask changes. All four
# libm bodies that differed -- sqrt, exp, expf and powf -- settle without it.
NEWLIB_CFLAGS="-g -Os -ffunction-sections -mcpu=cortex-m4 -mfloat-abi=hard -mfpu=fpv4-sp-d16 -mthumb"

if [ ! -d "$ROOT/src/$NEWLIB" ]; then
    [ -f "$ROOT/dl/$NEWLIB.tar.gz" ] || curl -L -o "$ROOT/dl/$NEWLIB.tar.gz" "$NEWLIB_URL"
    mkdir -p "$ROOT/src" && tar xf "$ROOT/dl/$NEWLIB.tar.gz" -C "$ROOT/src"
fi

# The nano options, as the Arm toolchain's own build script sets them, minus
# multilib (one library, the image's) and syscalls (the image has none: its
# _malloc_r and _free_r are shims onto FreeRTOS heap_4 and there is no sbrk).
NANO_OPTS="--target=arm-none-eabi --disable-multilib --disable-nls
 --disable-newlib-supplied-syscalls --enable-newlib-retargetable-locking
 --disable-newlib-fvwrite-in-streamio --disable-newlib-fseek-optimization
 --disable-newlib-wide-orient --enable-newlib-nano-malloc
 --disable-newlib-unbuf-stream-opt --enable-lite-exit
 --enable-newlib-global-atexit --enable-newlib-nano-formatted-io"

# NEWLIB_SRC is the tree a build configures from, so a patched copy is a
# variant rather than a second function.
NEWLIB_SRC=$ROOT/src/$NEWLIB

# The recipe a build directory was made from, written into it. A variant is
# named by its options, and the archive the relink takes bodies from has to be
# the archive the verdicts were measured against, so a directory whose recipe
# differs from the one the script now describes is rebuilt rather than reused.
# This is not hypothetical: newlib-nano-ll was once left configured against a
# scratch tree (~/ref-build/src/newlib-ll) that no longer exists, so nothing in
# the repo said what the archive the link was taking libc out of had been built
# from.
newlib_recipe() {
    printf 'src %s\ncc %s\ncflags %s\nopts %s\n' \
        "$NEWLIB_SRC" "$NEWLIB_CC" "$NEWLIB_CFLAGS" "$(echo $NANO_OPTS "$@")"
}

# An archive's code, in bytes: the file size is not a measurement, because every
# member carries the source path in its DWARF and a longer path is a bigger
# archive with identical text. text+data+bss of every member is the number that
# reproduces.
newlib_code_bytes() {
    PATH=$NEWLIB_CC:$PATH arm-none-eabi-size "$1" \
        | awk 'NR > 1 && NF >= 4 { s += $1 + $2 + $3 } END { print s + 0 }'
}

build_newlib() {
    # $1 is the build name, $2... any extra configure options.
    local name=$1; shift
    want "$name" || return 0
    local d=$OUT/$name
    if [ -f "$d/arm-none-eabi/newlib/libc.a" ]; then
        if [ "$(cat "$d/.recipe" 2>/dev/null)" = "$(newlib_recipe "$@")" ]; then
            return 0
        fi
        echo "newlib $name was built from another recipe; rebuilding"
    fi
    rm -rf "$d"; mkdir -p "$d"
    (cd "$d" && PATH=$NEWLIB_CC:$PATH "$NEWLIB_SRC/configure" \
        --prefix="$ROOT/install/$name" $NANO_OPTS "$@" \
        CFLAGS_FOR_TARGET="$NEWLIB_CFLAGS" > conf.log 2>&1)
    # libgloss wants the syscalls that are disabled, so it fails and is not
    # built; libc.a and libm.a are what this is for, and they are complete.
    (cd "$d" && PATH=$NEWLIB_CC:$PATH make -j"$(nproc)" > make.log 2>&1) || true
    [ -f "$d/arm-none-eabi/newlib/libc.a" ] || { echo "newlib $name did not build"; return 1; }
    newlib_recipe "$@" > "$d/.recipe"
    printf '%-16s %s\n' "$name" "$d/arm-none-eabi/newlib/libc.a"
}

# abi/newlib-sizes.txt records each archive's code bytes, and --check reports
# what is there against it. A variant whose recipe is pinned above and whose
# code bytes match is the archive abi/libc_check.py's verdicts and the relink's
# packed layout were measured against.
SIZES=$HERE/newlib-sizes.txt
check_newlib() {
    local bad=0 name archive want have
    while read -r name archive want; do
        [ -n "${name:-}" ] || continue
        case $name in \#*) continue ;; esac
        have=$(newlib_code_bytes "$OUT/$name/arm-none-eabi/newlib/$archive" 2>/dev/null || echo missing)
        if [ "$have" = "$want" ]; then
            printf '%-20s %-8s %10s  ok\n' "$name" "$archive" "$have"
        else
            printf '%-20s %-8s %10s  recorded %s\n' "$name" "$archive" "$have" "$want"
            bad=1
        fi
    done < "$SIZES"
    return $bad
}
NEWLIB_LL_SRC=$ROOT/src/$NEWLIB-longlong
if [ ! -d "$NEWLIB_LL_SRC" ]; then
    cp -r "$ROOT/src/$NEWLIB" "$NEWLIB_LL_SRC"
    for p in "$HERE"/patches/newlib/*.patch; do
        (cd "$NEWLIB_LL_SRC" && patch -p1 -s < "$p")
    done
fi
NEWLIB_SRC=$NEWLIB_LL_SRC
build_newlib newlib-nano-ll --enable-newlib-io-long-long
NEWLIB_SRC=$ROOT/src/$NEWLIB

# Every newlib archive the tree now holds, against abi/newlib-sizes.txt. The
# recipes above pin what each variant is configured from, so a difference here
# is a source or compiler change and not a stale build directory. `--check`
# stops at this report; the builds it passes through are no-ops on a tree that
# is already populated.
check_newlib
if [ "${1:-}" = --check ]; then exit 0; fi


# ---- libm as a matchable ELF ------------------------------------------------
# abi/match.py searches the image for library bodies and needs an object, not an
# archive; libc's verdicts come from abi/libc_check.py, which reads the archive
# directly, so `toolchain` is libm.
if want toolchain; then
    d=$OUT/toolchain
    mkdir -p "$d"
    "$GCC-ld" -r --whole-archive -o "$d/ref.elf" "$OUT/newlib-nano-ll/arm-none-eabi/newlib/libm.a"
    printf '%-14s %s\n' toolchain "$d/ref.elf"
fi

# ---- the Withings SAADC driver ---------------------------------------------
# The image's SAADC driver is nrfx 2.1.0, not the SDK's nrfx 1.9.0.
# abi/patches/nrfx/saadc-withings.patch carries the source changes and the
# evidence for each; abi/config-relink/nrfx_glue.h the two glue macros. The
# `app` build below compiles the driver out of this tree and abi/body_check.py
# checks the result against the image, which is what pins all three.
SAADC_SRC=$ROOT/nrfx/nrfx-2.1.0-withings
if [ -d "$ROOT/nrfx/nrfx-2.1.0" ] && [ ! -d "$SAADC_SRC" ]; then
    cp -r "$ROOT/nrfx/nrfx-2.1.0" "$SAADC_SRC"
    for p in "$HERE"/patches/nrfx/*.patch; do
        (cd "$SAADC_SRC" && patch -p1 -s < "$p")
    done
fi

# ---- the app's own build ----------------------------------------------------
# The kernel, port and driver abi/relink.sh links, measured against the image.
# nrfx 2.1.0 is the SAADC driver's tree alone: its other headers disagree with
# the SDK 17 integration layer the rest of the build compiles against, so the
# driver is compiled separately, with its own include path, and merged in. That
# is also how abi/relink.sh links it.
#
# -fcommon is GCC 9's default and GCC 13's is not; the SDK has tentative
# definitions (nrf_nvic_state) in two translation units and the partial link
# refuses them otherwise.
#
# REF_ASSERT=0 leaves configASSERT undefined. It is not a guess: the logging
# flavour (3) costs nine of the kernel bodies that otherwise reproduce, and the
# empty one (2) costs ten.
# APP_NAME/APP_EXTRA exist so a hypothesis about the image's compiler settings is
# a build directory of its own, measured beside `app` rather than replacing it.
APP_NAME=${APP_NAME:-app}
SKIP_SRC="modules/nrfx/drivers/src/nrfx_saadc.c"
build_variant "$APP_NAME" -Os -fcommon -DREF_ASSERT=0 -DconfigUSE_TIMERS=0 ${APP_EXTRA:-}
if want "$APP_NAME"; then
    SAADC_INC="-I$SAADC_SRC -I$SAADC_SRC/hal -I$SAADC_SRC/drivers -I$SAADC_SRC/drivers/include -I$SAADC_SRC/soc"
    # -fno-builtin is deliberately not passed: the image's channels_config calls
    # memset for the pselp/pseln reset, which only the builtin emits.
    "$GCC-gcc" $ARCH -ffunction-sections -fdata-sections \
        -fshort-enums -std=gnu99 -g3 -w -Os $DEFS -I"$HERE/config-relink" \
        $SAADC_INC $INC ${APP_EXTRA:-} \
        -c "$SAADC_SRC/drivers/src/nrfx_saadc.c" -o "$OUT/$APP_NAME/nrfx_saadc.o" 2>>"$OUT/$APP_NAME/err.log"
    "$GCC-ld" -r -o "$OUT/$APP_NAME/merged.elf" "$OUT/$APP_NAME/ref.elf" "$OUT/$APP_NAME/nrfx_saadc.o"
    mv "$OUT/$APP_NAME/merged.elf" "$OUT/$APP_NAME/ref.elf"
fi
SKIP_SRC=
