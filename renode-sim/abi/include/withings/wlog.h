/* HWA10 (ScanWatch 2) application firmware v3411: the wlog module's ABI.
 *
 * What each of these is, not where it is: abi/symbols.yaml holds the
 * addresses. Written by abi/migrate.py from the old single manifest and
 * hand-maintained since. */
#ifndef WITHINGS_WLOG_H
#define WITHINGS_WLOG_H

/* functions */
/* the app's printf into the wlog ring that reaches UART0; `push
   {r0,r1,r2,r3}` prologue is the AAPCS va_list spill, so it is callable as a
   plain variadic. Format strings carry their own trailing newline.
   */
extern void wlog(const char *fmt, ...);
/* inferred; r0 is a small level constant (5 at the 0x4029a call site), fmt
   in r1
   */
extern void wlog_level(unsigned int level, const char *fmt, ...);
/* NOT AAPCS-callable: the ring context comes in r12, so this is the sink the
   formatter installs, not an entry point. Listed for completeness; call
   wlog.
   */
extern void wlog_putchar(unsigned char c);

#endif
