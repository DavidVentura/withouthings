/* HWA10 (ScanWatch 2) application firmware v3411: the misc module's ABI.
 *
 * What each of these is, not where it is: abi/symbols.yaml holds the
 * addresses. Written by abi/migrate.py from the old single manifest and
 * hand-maintained since. */
#ifndef WITHINGS_MISC_H
#define WITHINGS_MISC_H

/* One passive measurement's schedule: the next deadline is the first word of
   the state block and its enable byte sits at +4; the sweep clears the deadline
   before it calls the handler, so a handler that wants to run again re-arms it.
   */
struct periodic_task_state {
    unsigned int deadline;
    unsigned char enabled;
};

/* One row of the passive schedule. The period is in seconds of uptime.
   */
struct periodic_task {
    unsigned int period;
    void (*run)(void);
    struct periodic_task_state *state;
};

/* globals */
/* the passive measurements the watch runs on its own. periodic_task_sweep walks
   the rows between the pool pair (0x27d64, 0x27d94) at a stride of 12 and calls
   a row's handler when its deadline has passed and its enable byte is set.
   Every row's period is 600, which is where the ten-minute cadence is written
   down; nothing over the wire changes it.
   */
extern struct periodic_task periodic_task_table[4];
extern unsigned int rtc1_overflow_count;
/* static TCB of the "main" task, not a pointer to one; declared opaque */
extern void *main_task_tcb;

#endif
