/* HWA10 (ScanWatch 2) application firmware v3411: the crown module's ABI.
 *
 * What each of these is, not where it is: abi/symbols.yaml holds the
 * addresses. Written by abi/migrate.py from the old single manifest and
 * hand-maintained since. */
#ifndef WITHINGS_CROWN_H
#define WITHINGS_CROWN_H

/* only the two fields at +0x10/+0x12 are identified */
struct crown_ctx {
    unsigned char pad[0x10];
    unsigned short idle_window_ms;
    unsigned short step_threshold;
};

/* functions */
/* inferred from the PAT9125 probe's id reads */
extern int crown_read_reg(unsigned char reg, unsigned char *out);
/* Delta_XY_Hi decode, negated. The caller at 0x2ee64 passes NULL for delta_y
   and 0x979d8 stores through it; machine.resc patches the sim.
   */
extern int crown_read_motion(short *delta_x, short *delta_y);
/* power-up sequence plus the 0x31/0x92 id gate */
extern int crown_probe(void);

/* globals */
extern struct crown_ctx crown_ctx;

#endif
