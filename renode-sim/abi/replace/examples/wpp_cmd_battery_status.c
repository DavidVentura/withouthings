/* WPP_CMD_BATTERY_STATUS (1284) rewritten in C against the generated header.
 *
 * Not wired into the relink: abi/replacements.yaml does not list it. It exists
 * to show that abi/include/withings alone carries everything a replacement of a WPP
 * command handler needs: the handler's prototype, the reply object's in-memory
 * struct, and a send path typed by that struct.
 *
 * Build:
 *   arm-none-eabi-gcc -mcpu=cortex-m4 -mthumb -mfloat-abi=hard \
 *     -mfpu=fpv4-sp-d16 -std=gnu11 -Wall -Wextra \
 *     -I abi/out -fsyntax-only abi/replace/examples/wpp_cmd_battery_status.c
 */

#include "withings/all.h"

void wpp_cmd_battery_status(const void *objects, unsigned short len)
{
    struct wpp_BatteryStatus reply = {0, 0, {0, 0}, 0, 0};
    unsigned char percent = 0;

    (void)objects;
    (void)len;

    battery_level_get(&percent);
    reply.battery_percent = percent;
    reply.battery_state = 2;
    battery_status_fill_state(&reply);
    reply.battery_mv = (unsigned int)battery_pct_to_mv(percent);

    wpp_send_BatteryStatus(0x504, &reply);
}
