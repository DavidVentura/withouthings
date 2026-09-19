/* HWA10 (ScanWatch 2) application firmware v3411: the bat module's ABI.
 *
 * What each of these is, not where it is: abi/symbols.yaml holds the
 * addresses. Written by abi/migrate.py from the old single manifest and
 * hand-maintained since. */
#ifndef WITHINGS_BAT_H
#define WITHINGS_BAT_H

struct wpp_BatteryStatus;

struct battery_curve_bounds {
    unsigned int pad;
    short full_mv;
    short empty_mv;
};

/* only +8 is identified: battery_measure_get tests it at 0x40284 and takes
   the fresh-conversion path when it is zero
   */
struct battery_state {
    unsigned char pad[0x8];
    unsigned short ready;
};

/* a table of string pointers indexed by a small code, the shape every
   *_names table in the image has
   */
struct name_ptr {
    const char *name;
};

/* functions */
/* SAADC AIN0; the gain adaptation is gated on product id 0x280010. Named by
   the __func__ the function logs, which symbols.txt already follows; the
   manifest still carried the older battery_adc_read.
   */
extern int battery_adc_value_gain_adaptation(void);
/* inferred from the WPP battery handler, which passes a single byte slot */
extern void battery_level_get(unsigned char *out_percent);
/* inferred; empty_mv + (percent * (full_mv - empty_mv) + 50) / 100, i.e. it
   turns a percentage into the millivolts the battery reply carries
   */
extern short battery_pct_to_mv(int percent);
/* inferred; reads STAT0, logs "VIN_PGOOD = %hu" */
extern int bq25180_status(void *dev);
/* inferred; sets/clears ICHG_CTRL bit 7 (CHG_DIS) */
extern int bq25180_charge_enable(void *dev, int enable);
/* I2C error -> 2, VIN_PGOOD=0 -> 0, else the table at 0xc6cbc */
extern int bat_charging_decision(void);
/* stores the charge state into out+1: 0 while the charger reports an error
   (0x922a2), otherwise 2 when the charger is idle and 1 or 3 from
   bat_charging_decision's verdict. Its only caller is
   wpp_cmd_battery_status, which is also what fixes its argument type.
   */
extern void battery_status_fill_state(struct wpp_BatteryStatus *out);

/* globals */
/* NULL until contrast_task's first run; the task the firmware creates as
   "CONTRAST" is the one that owns the battery loop */
extern void *contrast_sem;
/* in flash, read-only; the two int16 bounds battery_pct_rescale interpolates */
extern struct battery_curve_bounds battery_curve_bounds;
/* the block battery_measure_get (0x40274) works out of; its only pool word
   is 0x40348 and the `ldrh r3, [r5, #8]` at 0x40284 is the gate that decides
   whether the cached reading is usable or a fresh conversion is needed. Only
   that field is identified.
   */
extern struct battery_state battery_state;

/* tables */
/* The firmware's own name for every BQ25180 register, indexed by register
   address 0x00..0x0c. The shape is a pointer table, not the char[12] array
   the right-padded strings suggest: the reader at 0x435d2 loads this address
   and does `ldr r2, [r2, r4, lsl #2]` with r4 the register number, and the
   strings it points at are scattered (STAT0 0xd48f8, ICHG_CTRL 0xd4954 and
   CHARGECTRL1 0xd4979 sit among the log formats that also name them, the
   other ten run 0xd4a30..0xd4aa8). The names are padded to 11 columns
   because they are printed in one aligned column by the shell's `dump`.
   0xb2674 is where the run of string pointers starts and 0xb26a8 is where it
   stops; the four pointers below it are ble_state_names. Three call sites
   index it: 0x43600 (the register dump), 0x436ac and 0x43770/0x43784, all of
   which report through "[BQ25180] error reading %s" (0xd4917).
   */
extern struct name_ptr bq25180_reg_names[13];
/* the four colourised strings the "[BQ25180] CHG = %hu, %s" line selects
   between at 0x439fc, indexed by STAT0's two-bit CHG field: not charging,
   constant current, constant voltage, done/disabled
   */
extern struct name_ptr bq25180_charge_status_names[4];

#endif
