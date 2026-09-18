/* HWA10 (ScanWatch 2) application firmware v3411: the misc module's ABI.
 *
 * What each of these is, not where it is: abi/symbols.yaml holds the
 * addresses. Written by abi/migrate.py from the old single manifest and
 * hand-maintained since. */
#ifndef WITHINGS_MISC_H
#define WITHINGS_MISC_H

/* globals */
extern unsigned int rtc1_overflow_count;
/* static TCB of the "main" task, not a pointer to one; declared opaque */
extern void *main_task_tcb;

#endif
