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

/* One module's two post-update hooks. Both slots are optional and a null one
   is skipped, which is how a module that only marks reaches the table.
   */
struct post_update_hook {
    void (*check)(void);
    void (*mark)(void);
};

/* globals */
extern struct rambkp rambkp_block;
/* every hook that runs when a new firmware boots for the first time. Both
   readers load the bounds as a literal pair and step by eight, and the two
   columns are two passes over the same rows: post_update_run_checks calls +0
   before the module inits and post_update_run_marks calls +4 after them.
   cfs registers cfs_post_update_check in one column and cfs_post_update_mark
   in the other, which is what names them.
   */
extern struct post_update_hook post_update_hooks[11];
/* what a factory reset erases, one module per slot, null slots skipped.
   factory_reset_perform walks it with `ldr r3, [r5], #4` between the pair
   (0x27d34, 0x27d64) its own pool holds and calls each with no argument;
   cfs_factory_reset, glyph_cache_factory_reset and workout_cache_factory_reset
   are three of the twelve.
   */
extern void (*factory_reset_hooks[12])(void);

/* functions */
/* the first pass over post_update_hooks, under product_get_erase_flag, called
   from 0x2e3aa before the module inits.
   */
extern void post_update_run_checks(void);
/* the second pass, calling each row's +4, from 0x2e3e8 after them. */
extern void post_update_run_marks(void);
/* erases every module that registered in factory_reset_hooks; reboots through
   0x36cdc when mode is 2. WPP_CMD_FACTORY_RESET and shell_cmd_factory_reset
   are two of its three callers.
   */
extern void factory_reset_perform(unsigned int mode);

#endif
