#include "hwa10.h"

/* Placed first in the overlay so its entry point is the overlay's base address;
   the patch list hard-codes that address into the WPP dispatch table. */
__attribute__((section(".overlay_entry"), used))
void overlay_battery_hook(void *req)
{
    wlog("[OVERLAY] battery status, tick=%u full=%dmV empty=%dmV\n",
         xTickCount, (int)battery_curve_bounds.full_mv, (int)battery_curve_bounds.empty_mv);
    wpp_cmd_battery_status(req);
}
