/* HWA10 (ScanWatch 2) application firmware v3411: the freertos module's ABI.
 *
 * What each of these is, not where it is: abi/symbols.yaml holds the
 * addresses. Written by abi/migrate.py from the old single manifest and
 * hand-maintained since. */
#ifndef WITHINGS_FREERTOS_H
#define WITHINGS_FREERTOS_H

/* functions */
/* The body match anchors a start on a `bl` target, and nothing calls the
   idle task, so it settled on 0x27da6 and left the `push {r3-r7,lr}` at
   0x27da0 in a gap, six bytes the relayout was free to separate from the
   body. The entry is the xTaskCreateStatic at 0x73032, whose r0 pool word
   (0x73070) holds 0x27da1 and whose r1 names "IDLE".
   */
extern void prvIdleTask(void *pvParameters);
/* called with a NULL handle by battery_sem_give before battery_task exists;
   machine.resc patches the sim around it
   */
extern int xQueueGenericSend(void *xQueue, const void *pvItemToQueue, unsigned int xTicksToWait, int xCopyPosition);
/* pends PendSV via ICSR and returns — taskYIELD, NOT vTaskDelay. Anything
   calling this address as vTaskDelay(ticks) would silently not delay.
   */
/* inferred from the tickless-idle catch-up call site at 0x73f82 */
extern void vTaskStepTick(unsigned int xTicksToJump);
extern int xTaskIncrementTick(void);
/* twenty bytes, not the 316 the export gave it: it shifts its three
   arguments up one register, loads _impure_ptr through the pool word at
   0x8ebac and tail-calls _setenv_r with `b.w`. close_partition followed that
   tail call across the pool word instead of cutting on it, so the two bodies
   read as one function starting at 0x8eb9c and abi/autonames.py scored
   _setenv_r against the wrong start. Declared here so the boundary is
   pinned; both bodies then reproduce from the newlib build byte for byte at
   their own addresses.
   */
extern int setenv(const char *name, const char *value, int overwrite);
/* the body setenv tail-calls, which is where the newlib match belongs */
extern int _setenv_r(void *reent, const char *name, const char *value, int overwrite);
/* 2884 bytes that reproduce exactly from the newlib archive. The derivation
   could only call it __cvt__1, because __cvt at 0xa8268 is its only caller
   and a single-caller helper is all the call graph says; naming it puts it
   and its static quorem in the libc class instead of the helper one.
   */
extern char *_dtoa_r(void *reent, double value, int mode, int ndigits, int *decpt, int *sign, char **rve);
/* a libgcc body with its own archive section, declared so the partition pins
   its bounds: it and __fixunsdfdi sit inside the second tile of the split
   __assert_func__1__1 at 0x91ea4, which keeps both of them inside a tile
   that cannot be replaced.
   */
extern long long __fixdfdi(double value);
/* the second of the two libgcc bodies interleaved into 0x91ea4's tile */
extern unsigned long long __fixunsdfdi(double value);

/* globals */
extern unsigned int xTickCount;

#endif
