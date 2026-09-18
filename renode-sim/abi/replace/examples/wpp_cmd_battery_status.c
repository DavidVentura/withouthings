/* WPP_CMD_BATTERY_STATUS (1284) rewritten in C against the generated header.
 *
 * Not wired into the relink: abi/replacements.yaml does not list it. It exists
 * to show that abi/out/hwa10.h alone carries everything a replacement of a WPP
 * command handler needs -- the handler's prototype, the reply object's
 * in-memory struct, the codec that serialises it and the send path -- and to
 * name what it does not.
 *
 * Build:
 *   arm-none-eabi-gcc -mcpu=cortex-m4 -mthumb -mfloat-abi=hard \
 *     -mfpu=fpv4-sp-d16 -std=gnu11 -Wall -Wextra \
 *     -I abi/out -fsyntax-only abi/replace/examples/wpp_cmd_battery_status.c
 */

#include "hwa10.h"

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

    /* The table's function-pointer fields are typed, but the send path is not:
     * wpp_send_object_alias takes the erased wpp_obj_encoder/wpp_obj_sizer, so
     * the per-type codec has to be cast back to it at every reply site. */
    wpp_send_object_alias(&reply, 0x504,
                          wpp_obj_BatteryStatus_size,
                          (wpp_obj_encoder)wpp_obj_BatteryStatus_encode);
}
