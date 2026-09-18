/* HWA10 (ScanWatch 2) application firmware v3411: the boot module's ABI.
 *
 * What each of these is, not where it is: abi/symbols.yaml holds the
 * addresses. Written by abi/migrate.py from the old single manifest and
 * hand-maintained since. */
#ifndef WITHINGS_BOOT_H
#define WITHINGS_BOOT_H

/* mode at +20, attempt counter at +21, CRC at +32 */
struct rambkp {
    unsigned char pad[0x14];
    unsigned char boot_mode;
    unsigned char boot_attempts;
    unsigned char pad2[0xa];
    unsigned int crc;
};

/* globals */
extern struct rambkp rambkp_block;

#endif
