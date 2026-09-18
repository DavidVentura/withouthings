/* HWA10 (ScanWatch 2) application firmware v3411: the shell module's ABI.
 *
 * What each of these is, not where it is: abi/symbols.yaml holds the
 * addresses. Written by abi/migrate.py from the old single manifest and
 * hand-maintained since. */
#ifndef WITHINGS_SHELL_H
#define WITHINGS_SHELL_H

/* shell_cmd_entry's +4, called at 0x5a242..0x5a248 with the argument count
   from sp+4 and the argument vector at sp+8; the result is what the shell
   reports.
   */
typedef int (*shell_cmd_run)(int argc, char **argv);
/* shell_cmd_entry's +8, called at 0x5a25c with no argument set up and its
   result discarded, on the row whose argument strcmps equal to "--help"
   */
typedef void (*shell_cmd_help)(void);

/* both entry points carry the Thumb bit as stored. The walk at 0x5a224
   strcmps the argument against +0, calls +4 with (argc, argv), and calls +8
   instead when any argument is "--help" (0x5a278).
   */
struct shell_cmd_entry {
    const char *name;
    shell_cmd_run run;
    shell_cmd_help help;
};

/* tables */
/* the UART debug console's dispatch, sorted by command word so the walk can
   stop early. Head and end are the pair of literal-pool words every reader
   loads (0x27100 and 0x274b4 at 0x59cb8, 0x5a020, 0x5a134 and 0x5a270), and
   the stride is the reader's own `adds r5, #0xc` / `ldr r1, [r4], #12`; the
   end is where wpp_cmd_table begins, so the two tables abut. An earlier
   reading put the head at 0x270f8, which is inside the vector table and
   shifted every row's name onto the previous row's handler.
   */
extern struct shell_cmd_entry shell_cmd_table[79];

#endif
