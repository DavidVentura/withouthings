/* HWA10 (ScanWatch 2) application firmware v3411: the libgcc helpers the
 * image carries.
 *
 * No installed header declares these; GCC emits the calls itself. The ARM
 * run-time ABI fixes them to the base procedure call standard whatever the
 * float ABI of the caller, so a double travels in r0:r1 (and r2:r3) and a float
 * in r0 -- which is what the image's bodies read (__fixsfdi's `vmov s15, r0`,
 * __aeabi_dadd's `lsl.w r4, r1, #0x1`). The pcs attribute says so to the
 * compiler and to abi/ghidra/protos.py. */
#ifndef WITHINGS_LIBGCC_H
#define WITHINGS_LIBGCC_H

#define AEABI __attribute__((pcs("aapcs")))

/* double arithmetic; __aeabi_dadd is the adddf3 entry of ieee754-df.S, whose
   dsub entry the image carries under its libgcc name */
extern AEABI double __aeabi_dadd(double a, double b);
extern AEABI double __subdf3(double a, double b);
extern AEABI double __muldf3(double a, double b);
extern AEABI double __divdf3(double a, double b);

/* double comparisons: the libgcc forms answer in r0; the two cdcmple forms
   answer in the APSR flags alone, which C cannot say, so they return
   nothing */
extern AEABI int __gedf2(double a, double b);
extern AEABI int __ledf2(double a, double b);
extern AEABI int __nedf2(double a, double b);
extern AEABI int __unorddf2(double a, double b);
extern AEABI void __aeabi_cdcmple(double a, double b);
extern AEABI void __aeabi_cdrcmple(double a, double b);

/* conversions to and from double */
extern AEABI double __floatsidf(int i);
extern AEABI double __floatunsidf(unsigned int i);
extern AEABI double __floatdidf(long long i);
extern AEABI double __extendsfdf2(float f);
extern AEABI int __fixdfsi(double a);
extern AEABI unsigned int __fixunsdfsi(double a);
extern AEABI float __truncdfsf2(double a);

/* float arithmetic and conversions */
extern AEABI float __aeabi_fadd(float a, float b);
extern AEABI float __subsf3(float a, float b);
extern AEABI float __floatsisf(int i);
extern AEABI float __aeabi_ui2f(unsigned int i);
extern AEABI float __floatdisf(long long i);
extern AEABI float __aeabi_ul2f(unsigned long long i);
extern AEABI long long __fixsfdi(float a);
extern AEABI unsigned long long __fixunssfdi(float a);

/* 64-bit division: the quotient in r0:r1 and the remainder in r2:r3, a
   register pair C has no return type for; declared by the quotient */
extern long long __aeabi_ldivmod(long long numerator, long long denominator);
extern unsigned long long __aeabi_uldivmod(unsigned long long numerator,
                                           unsigned long long denominator);
extern unsigned long long __udivmoddi4(unsigned long long numerator,
                                       unsigned long long denominator,
                                       unsigned long long *remainder);

#endif
