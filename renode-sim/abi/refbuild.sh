#!/bin/bash
# Build reference objects for the open-source parts of the HWA10 firmware, so
# abi/match.py can map image addresses to source symbols.
#
#   abi/refbuild.sh          # downloads (once) into ~/ref-build, builds every variant
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
#   GNU Arm Embedded 9-2020-q2-update (gcc 9.3.1 20200408), the compiler the SDK
#     17.1.0 armgcc makefiles name in Makefile.posix
#     https://developer.arm.com/-/media/Files/downloads/gnu-rm/9-2020q2/gcc-arm-none-eabi-9-2020-q2-update-x86_64-linux.tar.bz2
#
# The config is derived from observed firmware behaviour; see abi/CONFIG-HUNT in
# the commit message and the notes in matches.yaml.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=${ROOT:-$HOME/ref-build}
SDK=$ROOT/sdk/nRF5_SDK_17.1.0_ddde560
GCC=$ROOT/gcc-arm-none-eabi-9-2020-q2-update/bin/arm-none-eabi
OUT=$ROOT/build
CFG=$ROOT/cfg

SDK_URL=https://files.nordicsemi.com/artifactory/nRF5-SDK/external/nRF5_SDK_v17.x.x/nRF5_SDK_17.1.0_ddde560.zip
GCC_URL=https://developer.arm.com/-/media/Files/downloads/gnu-rm/9-2020q2/gcc-arm-none-eabi-9-2020-q2-update-x86_64-linux.tar.bz2
# Arm GNU Toolchain releases whose prebuilt newlib is matched against the image
# by abi/autonames.py. The image names its own newlib in three source paths
# (.../newlib-4.3.0.20230120/newlib/libc/stdlib/{dtoa,mprec,gdtoa-gethex}.c),
# and 12.3.Rel1 and 13.2.Rel1 are the releases that ship exactly that newlib;
# 13.3 and 14.2 ship 4.4.0 and are fetched only as controls for the GCC-version
# comparison, since their newlib cannot be the image's.
TC_RELEASES="12.3.rel1 13.2.rel1 13.3.rel1 14.2.rel1"
TC_BASE=https://developer.arm.com/-/media/Files/downloads/gnu

mkdir -p "$ROOT/dl"
if [ ! -d "$SDK" ]; then
    [ -f "$ROOT/dl/sdk.zip" ] || curl -L -o "$ROOT/dl/sdk.zip" "$SDK_URL"
    mkdir -p "$ROOT/sdk" && unzip -q "$ROOT/dl/sdk.zip" -d "$ROOT/sdk"
fi
if [ ! -d "$(dirname "$GCC")" ]; then
    [ -f "$ROOT/dl/gcc.tar.bz2" ] || curl -L -o "$ROOT/dl/gcc.tar.bz2" "$GCC_URL"
    tar xf "$ROOT/dl/gcc.tar.bz2" -C "$ROOT"
fi
mkdir -p "$ROOT/tc"
for v in $TC_RELEASES; do
    if ! ls -d "$ROOT/tc/arm-gnu-toolchain-$v"* >/dev/null 2>&1; then
        f="$ROOT/dl/arm-gnu-toolchain-$v.tar.xz"
        [ -f "$f" ] || curl -L -o "$f" "$TC_BASE/$v/binrel/arm-gnu-toolchain-$v-x86_64-arm-none-eabi.tar.xz"
        tar xf "$f" -C "$ROOT/tc"
    fi
done

mkdir -p "$OUT" "$CFG"

# ---- FreeRTOSConfig.h -------------------------------------------------------
# Every non-default value below is forced by something traced in the image; the
# reasoning is in the commit message.
cat > "$CFG/FreeRTOSConfig.h" <<'EOF'
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#include "nrf.h"
#include "nrf_assert.h"

#define FREERTOS_USE_RTC      0
#define FREERTOS_USE_SYSTICK  1
#define configTICK_SOURCE     FREERTOS_USE_RTC

#define configUSE_PREEMPTION                    1
#ifndef configUSE_PORT_OPTIMISED_TASK_SELECTION
#define configUSE_PORT_OPTIMISED_TASK_SELECTION 1
#endif
#define configUSE_TICKLESS_IDLE                 1
#define configUSE_TICKLESS_IDLE_SIMPLE_DEBUG    1
#define configCPU_CLOCK_HZ                      ( SystemCoreClock )
#define configTICK_RATE_HZ                      1000
#ifndef configMAX_PRIORITIES
#define configMAX_PRIORITIES ( 5 )
#endif
#define configMINIMAL_STACK_SIZE                ( 60 )
#define configTOTAL_HEAP_SIZE                   ( 16384 )
#ifndef configMAX_TASK_NAME_LEN
#define configMAX_TASK_NAME_LEN ( 12 )
#endif
#define configRECORD_STACK_HIGH_ADDRESS         1
#define configUSE_16_BIT_TICKS                  0
#define configIDLE_SHOULD_YIELD                 1
#define configUSE_MUTEXES                       1
#define configUSE_RECURSIVE_MUTEXES             1
#define configUSE_COUNTING_SEMAPHORES           1
#define configUSE_ALTERNATIVE_API               0
#ifndef configQUEUE_REGISTRY_SIZE
#define configQUEUE_REGISTRY_SIZE 0
#endif
#define configUSE_QUEUE_SETS                    1
#ifndef configUSE_TIME_SLICING
#define configUSE_TIME_SLICING 1
#endif
#define configUSE_NEWLIB_REENTRANT              0
#ifndef configENABLE_BACKWARD_COMPATIBILITY
#define configENABLE_BACKWARD_COMPATIBILITY 0
#endif
#define configSUPPORT_STATIC_ALLOCATION         1
#define configSUPPORT_DYNAMIC_ALLOCATION        1
#define configUSE_TASK_NOTIFICATIONS            1
#define configUSE_IDLE_HOOK                     0
#define configUSE_TICK_HOOK                     1
#ifndef configCHECK_FOR_STACK_OVERFLOW
#define configCHECK_FOR_STACK_OVERFLOW 2
#endif
#define configUSE_MALLOC_FAILED_HOOK            0
#ifndef configGENERATE_RUN_TIME_STATS
#define configGENERATE_RUN_TIME_STATS 0
#endif
#ifndef configUSE_TRACE_FACILITY
#define configUSE_TRACE_FACILITY 0
#endif
#define configUSE_STATS_FORMATTING_FUNCTIONS    0
#define configUSE_CO_ROUTINES                   0
#define configMAX_CO_ROUTINE_PRIORITIES         ( 2 )
#ifndef configUSE_TIMERS
#define configUSE_TIMERS 1
#endif
#define configTIMER_TASK_PRIORITY               ( 2 )
#define configTIMER_QUEUE_LENGTH                32
#define configTIMER_TASK_STACK_DEPTH            ( 80 )
#define configEXPECTED_IDLE_TIME_BEFORE_SLEEP   2

/* REF_ASSERT picks the assert flavour; the variants build both, because which
   one the image used is only decidable by matching. __builtin_trap() is the udf
   the image's assert path ends in. */
#if REF_ASSERT == 1
#define configASSERT( x ) do { if( !( x ) ) { __builtin_trap(); } } while( 0 )
#elif REF_ASSERT == 2
#define configASSERT( x ) ( ( void ) 0 )
#elif REF_ASSERT == 3
/* The image's assert is a logging call taking __FILE__ in r0 and __LINE__ in r1,
   followed by an infinite loop (e.g. xQueueGenericSend @0x73aa4). */
extern void ref_assert_log( const char *file, unsigned int line );
#define configASSERT( x ) do { if( !( x ) ) { ref_assert_log( __FILE__, __LINE__ ); for( ;; ) {} } } while( 0 )
#endif
/* REF_ASSERT == 0 leaves configASSERT undefined, which also drops the
   configASSERT_DEFINED code (the volatile sizeof(StaticTask_t) store in
   xTaskCreateStatic) -- the image has no such store. */

#define INCLUDE_vTaskPrioritySet                1
#define INCLUDE_uxTaskPriorityGet               1
#define INCLUDE_vTaskDelete                     1
#define INCLUDE_vTaskSuspend                    1
#define INCLUDE_xResumeFromISR                  1
#define INCLUDE_vTaskDelayUntil                 1
#define INCLUDE_vTaskDelay                      1
#define INCLUDE_xTaskGetSchedulerState          1
#define INCLUDE_xTaskGetCurrentTaskHandle       1
#define INCLUDE_uxTaskGetStackHighWaterMark     1
#define INCLUDE_xTaskGetIdleTaskHandle          1
#define INCLUDE_xTimerGetTimerDaemonTaskHandle  1
#define INCLUDE_pcTaskGetTaskName               1
#define INCLUDE_eTaskGetState                   1
#define INCLUDE_xEventGroupSetBitFromISR        1
#define INCLUDE_xTimerPendFunctionCall          1

/* basepri 0xe0 kernel / 0xc0 max-syscall as traced, with 3 priority bits. */
#define configLIBRARY_LOWEST_INTERRUPT_PRIORITY      0x7
#define configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY 0x6
#define configKERNEL_INTERRUPT_PRIORITY      configLIBRARY_LOWEST_INTERRUPT_PRIORITY
#define configMAX_SYSCALL_INTERRUPT_PRIORITY configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY

#define vPortSVCHandler     SVC_Handler
#define xPortPendSVHandler  PendSV_Handler
/* RTC2, not the SDK example's RTC1: rtc2_init @0x74050 writes PRESCALER 0x20. */
#define xPortSysTickHandler RTC2_IRQHandler
#define configSYSTICK_CLOCK_HZ ( 32768UL )

#if !(defined(__ASSEMBLY__) || defined(__ASSEMBLER__))
#include "nrf.h"
#ifdef __NVIC_PRIO_BITS
#define configPRIO_BITS __NVIC_PRIO_BITS
#else
#error "This port requires __NVIC_PRIO_BITS to be defined"
#endif
#endif

#define configUSE_DISABLE_TICK_AUTO_CORRECTION_DEBUG 0

#endif
EOF

# ---- include paths ----------------------------------------------------------
INC="-I$CFG"
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
  external/freertos/portable/GCC/nrf52 \
  external/freertos/portable/CMSIS/nrf52 \
  external/fprintf ; do
    INC="$INC -I$SDK/$d"
done
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

SRC="
external/freertos/portable/GCC/nrf52/port.c
external/freertos/portable/CMSIS/nrf52/port_cmsis.c
external/freertos/portable/CMSIS/nrf52/port_cmsis_systick.c
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
COMMON="-ffunction-sections -fdata-sections -fno-strict-aliasing -fno-builtin
 -fshort-enums -std=gnu99 -g3 -w"

# KERNEL points at a FreeRTOS kernel source root: the SDK's own copy, or an
# upstream FreeRTOS-Kernel tarball, since which version Withings used is part of
# the hunt (the SDK's copy calls itself V9.0.0).
KERNEL=${KERNEL:-$SDK/external/freertos/source}
# NRFX likewise: the image's driver control blocks do not have the SDK 17 (nrfx 2.x)
# layout, so older nrfx trees are built as their own variants when present.
NRFX=${NRFX:-}

build_variant() {
    local name="$1"; shift
    local od="$OUT/$name"
    rm -rf "$od"; mkdir -p "$od"
    local objs=""
    local kinc="-I$KERNEL/include"
    if [ -n "$NRFX" ]; then
        kinc="$kinc -I$NRFX -I$NRFX/hal -I$NRFX/drivers -I$NRFX/drivers/include -I$NRFX/soc -I$NRFX/mdk"
    fi
    for s in $KERNEL_SRC; do
        local o="$od/kernel_$(echo "$s" | tr / _ | sed 's/\.c$/.o/')"
        if "$GCC-gcc" $ARCH $COMMON "$@" $DEFS $kinc $INC -c "$KERNEL/$s" -o "$o" 2>>"$od/err.log"; then
            objs="$objs $o"
        else
            echo "  skip $KERNEL/$s" >> "$od/skipped.log"
        fi
    done
    INC="$kinc $INC"
    for s in $SRC; do
        local o="$od/$(echo "$s" | tr / _ | sed 's/\.c$/.o/')"
        local src="$SDK/$s"
        case "$s" in modules/nrfx/*) [ -z "$NRFX" ] || src="$NRFX/${s#modules/nrfx/}" ;; esac
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

# The knobs below are what the configuration hunt converged on; see the commit
# message. TCB_t in the image is 0x60 bytes with ucNotifyState at +0x5c, which is
# 12 bytes more than the baseline: configUSE_TRACE_FACILITY adds uxTCBNumber and
# uxTaskNumber (8) and configUSE_APPLICATION_TASK_TAG adds pxTaskTag (4).
# The four knobs above the hunt -- queue sets, a 12-byte task name, the recorded
# stack high address and the tick hook -- are read straight off the image's own
# struct layouts and call sites, so they are not variants; abi/boundary.yaml
# carries the evidence.
HUNT="-DREF_ASSERT=0 -DconfigUSE_TRACE_FACILITY=1 -DconfigUSE_APPLICATION_TASK_TAG=1"
TCB="-DconfigUSE_TRACE_FACILITY=1 -DconfigUSE_APPLICATION_TASK_TAG=1"
for o in Os O2 O3; do
    for a in 0 2 3; do
        build_variant "${o}_a${a}" "-$o" "-DREF_ASSERT=$a" $TCB
    done
done

# ---- mbedTLS ----------------------------------------------------------------
# The image contains mbedTLS (its oid.c description strings -- "ecdsa-with-SHA256",
# "TLS Web Client Authentication" -- are in the flash verbatim), and the WPPS
# characteristic drives a TLS handshake. The SDK's own copy is 2.16.10, which is
# the first candidate version; it builds standalone against its default config.
build_mbedtls() {
    local name="$1"; shift
    local od="$OUT/$name"
    local src="$SDK/external/mbedtls"
    [ -d "$src" ] || return 0
    rm -rf "$od"; mkdir -p "$od"
    local objs=""
    for s in "$src"/library/*.c; do
        local o="$od/$(basename "$s" .c).o"
        if "$GCC-gcc" $ARCH $COMMON "$@" -I"$src/include" -c "$s" -o "$o" 2>>"$od/err.log"; then
            objs="$objs $o"
        else
            echo "  skip $s" >> "$od/skipped.log"
        fi
    done
    "$GCC-ld" -r -o "$od/ref.elf" $objs
    printf '%-10s %3d objs  %s\n' "$name" "$(echo $objs | wc -w)" "$od/ref.elf"
}
build_mbedtls mbedtls_Os -Os
build_mbedtls mbedtls_O2 -O2

# Older nrfx trees, if they were fetched next to the SDK.
for n in "$ROOT"/nrfx/nrfx-*; do
    [ -d "$n" ] || continue
    case "$n" in *-withings) continue ;; esac   # built below, by its own compiler
    ver=$(basename "$n" | sed 's/nrfx-//;s/\./_/g')
    NRFX=$n
    build_variant "n${ver}_Os" -Os "-DREF_ASSERT=0" $TCB
    build_variant "n${ver}_O2" -O2 "-DREF_ASSERT=0" $TCB
done
NRFX=

# Upstream kernel versions, if they were fetched next to the SDK.
for k in "$ROOT"/krn/FreeRTOS-Kernel-*; do
    [ -d "$k" ] || continue
    ver=$(basename "$k" | sed 's/FreeRTOS-Kernel-//;s/\./_/g')
    KERNEL=$k
    for o in Os O2; do
        for a in 0 3; do
            build_variant "k${ver}_${o}_a${a}" "-$o" "-DREF_ASSERT=$a" $TCB
        done
    done
done

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
NEWLIB_CFLAGS="-g -Os -ffunction-sections -fdata-sections -mcpu=cortex-m4 -mfloat-abi=hard -mfpu=fpv4-sp-d16 -mthumb"

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
build_newlib newlib-nano-big
build_newlib newlib-nano-small --enable-newlib-reent-small --enable-newlib-reent-check-verify

# The image's printf and scanf take long long, which no release of newlib can
# do in the nano files: nano-vfprintf_local.h and nano-vfscanf_local.h define
# _NO_LONGLONG unconditionally in 4.3.0 and still do in 4.5.0, and no Arm
# prebuilt nano archive differs there. abi/patches/newlib/ makes those two
# headers honour --enable-newlib-io-long-long the way the non-nano files
# already do and adds the two bodies the image shows: the second 'l' consumed
# into QUADINT in nano-vfprintf.c, and the _strtoll_r/_strtoull_r arm in
# nano-vfscanf_i.c. With it _svfprintf_r (0x90fb8) and _scanf_i (0x91780)
# reproduce the image byte for byte, which they do under no other
# configuration tried: -O2/-O3, GCC 12.3/13.2/13.3/14.2, newlib 4.4.0/4.5.0,
# reent-small, and the io-c99-formats/io-pos-args/multithread options.
NEWLIB_LL_SRC=$ROOT/src/$NEWLIB-longlong
if [ ! -d "$NEWLIB_LL_SRC" ]; then
    cp -r "$ROOT/src/$NEWLIB" "$NEWLIB_LL_SRC"
    for p in "$HERE"/patches/newlib/*.patch; do
        (cd "$NEWLIB_LL_SRC" && patch -p1 -s < "$p")
    done
fi
NEWLIB_SRC=$NEWLIB_LL_SRC
build_newlib newlib-nano-ll --enable-newlib-io-long-long

# Single-thread, tried and refused. abi/libc_check.py reports that the image's
# __swbuf_r calls the 36-byte unlocked fflush at 0xa8fd0 where this build's
# __swbuf_r calls the 74-byte locking _fflush_r, and the 74-byte body is
# nowhere in the image at all, which reads like --disable-newlib-multithread.
# It is not: that option takes the _lock member out of FILE, every fake FILE a
# stdio wrapper builds on its own stack loses a word, and sprintf, snprintf and
# sscanf stop reproducing on nothing but stack offsets shifted by four
# (sprintf's eleven differing bytes are `str r0,[sp,#8]` against `[sp,#4]`, and
# so on through the body). The image's frames are the wide ones, so its FILE
# carries the lock and the library is multithread, the same structural argument
# that settled _REENT_SMALL. Measured: 83 of 115 libc bodies reproduce under
# --disable-newlib-multithread against 87 under the default, it fixes neither
# __swbuf_r nor __assert_func, and it breaks four bodies that reproduced. What
# the image does with fflush is narrower than a configure switch and is still
# open; abi/out/relink/libc-bodies.yaml carries it as __swbuf_r's verdict.
build_newlib newlib-nano-ll-st --enable-newlib-io-long-long --disable-newlib-multithread
NEWLIB_SRC=$ROOT/src/$NEWLIB

# Every newlib archive the tree now holds, against abi/newlib-sizes.txt. The
# recipes above pin what each variant is configured from, so a difference here
# is a source or compiler change and not a stale build directory. `--check`
# stops at this report; the builds it passes through are no-ops on a tree that
# is already populated.
check_newlib
if [ "${1:-}" = --check ]; then exit 0; fi

# ---- the Withings SAADC driver ---------------------------------------------
# The image's SAADC driver is nrfx 2.1.0, not the SDK's nrfx 1.9.0, built by a
# different compiler from the rest of the variants above: the same GCC 13.2 that
# builds the image's newlib. abi/patches/nrfx/saadc-withings.patch carries the
# source changes and the evidence for each; abi/config-relink/nrfx_glue.h the two
# glue macros. abi/body_check.py checks the result against the image, which is
# what pins all three.
SAADC_SRC=$ROOT/nrfx/nrfx-2.1.0-withings
if [ -d "$ROOT/nrfx/nrfx-2.1.0" ] && [ ! -d "$SAADC_SRC" ]; then
    cp -r "$ROOT/nrfx/nrfx-2.1.0" "$SAADC_SRC"
    for p in "$HERE"/patches/nrfx/*.patch; do
        (cd "$SAADC_SRC" && patch -p1 -s < "$p")
    done
fi

build_saadc() {
    local name="$1" src="$2"
    local od="$OUT/$name"
    [ -d "$src" ] || return 0
    rm -rf "$od"; mkdir -p "$od"
    local cc=$ROOT/tc/arm-gnu-toolchain-13.2.Rel1-x86_64-arm-none-eabi/bin/arm-none-eabi
    local ninc="-I$src -I$src/hal -I$src/drivers -I$src/drivers/include -I$src/soc -I$src/mdk"
    # -fno-builtin is deliberately not passed: the image's channels_config calls
    # memset for the pselp/pseln reset, which only the builtin emits.
    local common="-ffunction-sections -fdata-sections -fno-strict-aliasing
     -fshort-enums -std=gnu99 -g3 -w -Os"
    "$cc-gcc" $ARCH $common $DEFS -I"$HERE/config-relink" $ninc $INC \
        -c "$src/drivers/src/nrfx_saadc.c" -o "$od/nrfx_saadc.o" 2>"$od/err.log" || {
        echo "$name did not build; see $od/err.log"; return 1; }
    "$cc-ld" -r -o "$od/ref.elf" "$od/nrfx_saadc.o"
    printf '%-14s %s\n' "$name" "$od/ref.elf"
}
build_saadc saadc_stock     "$ROOT/nrfx/nrfx-2.1.0"
build_saadc saadc_withings  "$SAADC_SRC"

# ---- CMSIS-DSP and KissFFT --------------------------------------------------
# Candidates for the float and signal-processing blocks inside SENSORS_SYNC and
# ECG, which are hard-float VFP code no struct layout reaches. An earlier verdict
# ruled CMSIS-DSP out from its constant tables alone; these variants put the
# bodies in front of abi/match.py. Releases span the plausible build window:
# CMSIS 5.7.0 (DSP 1.9.0), CMSIS 5.9.0 (DSP 1.10.0) and the standalone v1.14.4.
DSP_SRC=$ROOT/src/dsp
DSP_TAGS="5.7.0 5.9.0 1.14.4"
DSP_URL=https://github.com/ARM-software/CMSIS-DSP/archive/refs/tags
KISS_URL=https://github.com/mborgerding/kissfft/archive/refs/tags/131.1.0.tar.gz

mkdir -p "$DSP_SRC"
for t in $DSP_TAGS; do
    [ -d "$DSP_SRC/CMSIS-DSP-$t" ] && continue
    f=$ROOT/dl/cmsis-dsp-$t.tar.gz
    v=$t; case "$t" in 1.*) v=v$t ;; esac
    [ -s "$f" ] || curl -L -o "$f" "$DSP_URL/$v.tar.gz"
    tar xf "$f" -C "$DSP_SRC"
done
if [ ! -d "$DSP_SRC/kissfft-131.1.0" ]; then
    [ -s "$ROOT/dl/kissfft-131.1.0.tar.gz" ] || curl -L -o "$ROOT/dl/kissfft-131.1.0.tar.gz" "$KISS_URL"
    tar xf "$ROOT/dl/kissfft-131.1.0.tar.gz" -C "$DSP_SRC"
fi

# ARM_MATH_CM4 with __FPU_PRESENT is the image's core; the CMSIS-Core headers
# come from the SDK, which is where the image's own would have come from.
build_cmsisdsp() {
    local name="$1" src="$2"; shift 2
    local od="$OUT/$name"
    [ -d "$src" ] || return 0
    rm -rf "$od"; mkdir -p "$od"
    local dinc="-I$src/Include -I$src/PrivateInclude -I$SDK/components/toolchain/cmsis/include"
    local ddef="-DARM_MATH_CM4 -D__FPU_PRESENT=1 -DNRF52840_XXAA -D__GNUC_PYTHON__=0"
    local objs=""
    for s in $(find "$src/Source" -name '*.c' | sort); do
        # Each Source subdirectory carries an umbrella .c that #includes all its
        # siblings, so compiling it too gives every body twice.
        [ "$(basename "$s" .c)" = "$(basename "$(dirname "$s")")" ] && continue
        local o="$od/$(echo "${s#$src/Source/}" | tr / _ | sed 's/\.c$/.o/')"
        if "$GCC-gcc" $ARCH $COMMON "$@" $ddef $dinc -c "$s" -o "$o" 2>>"$od/err.log"; then
            objs="$objs $o"
        else
            echo "  skip $s" >> "$od/skipped.log"
        fi
    done
    [ -n "$objs" ] || return 0
    "$GCC-ld" -r -o "$od/ref.elf" $objs
    printf '%-14s %3d objs  %s\n' "$name" "$(echo $objs | wc -w)" "$od/ref.elf"
}
for t in $DSP_TAGS; do
    tag=$(echo "$t" | tr . _)
    build_cmsisdsp "dsp${tag}_Os" "$DSP_SRC/CMSIS-DSP-$t" -Os
    build_cmsisdsp "dsp${tag}_O2" "$DSP_SRC/CMSIS-DSP-$t" -O2
done

build_kissfft() {
    local name="$1"; shift
    local src=$DSP_SRC/kissfft-131.1.0
    local od="$OUT/$name"
    [ -d "$src" ] || return 0
    rm -rf "$od"; mkdir -p "$od"
    local objs=""
    for s in "$src"/kiss_fft.c "$src"/kiss_fftr.c "$src"/kiss_fftnd.c "$src"/kiss_fftndr.c; do
        local o="$od/$(basename "$s" .c).o"
        if "$GCC-gcc" $ARCH $COMMON "$@" -DFIXED_POINT=0 -I"$src" -c "$s" -o "$o" 2>>"$od/err.log"; then
            objs="$objs $o"
        fi
    done
    [ -n "$objs" ] || return 0
    "$GCC-ld" -r -o "$od/ref.elf" $objs
    printf '%-14s %3d objs  %s\n' "$name" "$(echo $objs | wc -w)" "$od/ref.elf"
}
build_kissfft kissfft_Os -Os
build_kissfft kissfft_O2 -O2

# ---- newlib libm as a matchable ELF ----------------------------------------
# abi/autonames.py matches the Arm prebuilt libm.a; these turn the libm built
# here (the full-precision one, from Withings' own newlib version) into a
# ref.elf so abi/match.py can search the SENSORS_SYNC and ECG blocks for it.
for n in newlib-nano-big newlib-nano-small; do
    a=$OUT/$n/arm-none-eabi/newlib/libm.a
    [ -f "$a" ] || continue
    d=$OUT/libm_${n#newlib-}
    mkdir -p "$d"
    "$GCC-ld" -r --whole-archive -o "$d/ref.elf" "$a"
    printf '%-14s %s\n' "libm_${n#newlib-}" "$d/ref.elf"
done
