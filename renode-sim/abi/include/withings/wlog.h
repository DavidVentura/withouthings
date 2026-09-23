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
   plain variadic. Format strings carry their own trailing newline. It is
   newlib's printf, _vfprintf_r (0x914bc) on _impure_ptr's stdout, whose count
   it returns.
   */
extern int wlog(const char *fmt, ...);
/* r0 is a small level constant (5 at the 0x4029a call site); r1 a 32-bit
   log id (0xea633770 beside "[M] reset_reason : 0x%x\n" at 0x2e466), stored
   raw into the binary record at +0xf; r2 the format, which is also what the
   record's arguments are scanned against. The source takes the format as its
   first va_arg (`push {r2, r3}`); as a named third argument it lands in the
   same place. Always answers 0.
   */
extern int wlog_level(unsigned int level, unsigned int log_id, const char *fmt, ...);
/* NOT AAPCS-callable: the ring context comes in r12, so this is the sink the
   formatter installs, not an entry point. Listed for completeness; call
   wlog.
   */
extern void wlog_putchar(unsigned char c);

#endif
