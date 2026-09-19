/* The same replacement, with a global of its own.
 *
 * The version it answers is the image's, so nothing a run prints changes; what
 * is new is that the body keeps state, in RAM the linker chose rather than in
 * a word the image already had. Before the app's RAM was sections there was
 * nowhere for it to go: a replacement could only borrow a static the blob
 * already owned, through a stand-in on the blob's own copy. */

#include "replace.h"

unsigned int get_fw_version_calls;

unsigned int get_fw_version(void)
{
    get_fw_version_calls += 1;
    return appl_fw_version;
}
