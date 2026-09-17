/* The B side of the A/B pair: the same replacement with a body that answers a
 * version the image does not carry, so every log line and every reply that goes
 * through get_fw_version reads the replacement and not the original. */

#include "replace.h"

unsigned int get_fw_version(void)
{
    return 9999;
}
