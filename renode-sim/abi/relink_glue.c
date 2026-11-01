/* What the source FreeRTOS needs and the blob does not export.
 *
 * Everything here is a fact about this firmware, established in
 * abi/boundary.yaml or symbols.txt, not a stand-in. */

#include "nrf.h"
#include "nrf_soc.h"
#include "nrf_nvic.h"
#include "FreeRTOS.h"
#include "task.h"

/* The blob's Reset_Handler zeroes only the blob's own .bss, so the source
 * library's statics would start as whatever the flash-erased RAM held. Reset_Handler's first call is
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
