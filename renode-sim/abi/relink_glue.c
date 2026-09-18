/* What the source FreeRTOS needs and the blob does not export.
 *
 * Everything here is a fact about this firmware, established in
 * abi/boundary.yaml or symbols.txt, not a stand-in. */

#include "nrf.h"
#include "nrf_soc.h"
#include "nrf_nvic.h"
#include "FreeRTOS.h"
#include "task.h"

nrf_nvic_state_t nrf_nvic_state;

void vAssertCalled(const char *file, unsigned int line);

/* The image calls portYIELD out of line (0x9e46c, 77 call sites) and without
 * the __SEV() the SDK's macro inlines; DEVELOPMENT.md records that mismatch. */
void vPortYield(void)
{
    SCB->ICSR = SCB_ICSR_PENDSVSET_Msk;
    __DSB();
    __ISB();
}

/* port.c has this but keeps it static, and the blob calls it across the
 * boundary (0x73dfc). */
__attribute__((naked)) void vPortEnableVFP(void)
{
    __asm volatile
    (
        "   ldr.w r0, =0xE000ED88   \n"
        "   ldr r1, [r0]            \n"
        "   orr r1, r1, #(0xf << 20)\n"
        "   str r1, [r0]            \n"
        "   bx r14                  \n"
        "   .align 4                \n"
    );
}

/* port_cmsis_systick.c reaches for nrf_drv_clock and nrf_sdh; the image's own
 * tick setup (0x74034) starts the LFCLK by hand instead, so do that. */
void nrf_drv_clock_lfclk_request(void *handler)
{
    (void)handler;
    if (NRF_CLOCK->LFCLKSTAT & CLOCK_LFCLKSTAT_STATE_Msk) {
        return;
    }
    NRF_CLOCK->EVENTS_LFCLKSTARTED = 0;
    NRF_CLOCK->TASKS_LFCLKSTART = 1;
    while (NRF_CLOCK->EVENTS_LFCLKSTARTED == 0) {
    }
}

/* The image runs with S140 7.3.0 enabled from appl_main_init (0x2e50c). */
unsigned char nrf_sdh_is_enabled(void)
{
    return 1;
}

void app_error_handler_bare(unsigned int error_code)
{
    vAssertCalled("app_error", error_code);
    for (;;) {
    }
}

/* The blob's Reset_Handler zeroes only the blob's own .bss, so the source
 * library's statics -- 16 KB of them, nearly all heap_4's ucHeap -- would start
 * as whatever the flash-erased RAM held. Reset_Handler's first call is
 * SystemInit (0x34968 -> 0x34854), well before any kernel use, and
 * abi/boundary.yaml's `startup` list diverts it here.
 *
 * The store is through a volatile pointer so -Os cannot turn the loop back into
 * a call to memset: the app's memset is not on this side of the boundary. */
extern unsigned int __libbss_start[];
extern unsigned int __libbss_end[];
extern unsigned int __libdata_start[];
extern unsigned int __libdata_end[];
extern unsigned int __libdata_load[];

void appl_SystemInit(void);

void relink_startup(void)
{
    const unsigned int *from = __libdata_load;

    for (volatile unsigned int *p = __libdata_start; p != __libdata_end; p++) {
        *p = *from++;
    }
    for (volatile unsigned int *p = __libbss_start; p != __libbss_end; p++) {
        *p = 0;
    }
    appl_SystemInit();
}
