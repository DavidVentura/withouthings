/* get_fw_version, reimplemented against the same trailer word the blob's copy
 * loads through its literal pool. */

#include "replace.h"

unsigned int get_fw_version(void)
{
    return appl_fw_version;
}
