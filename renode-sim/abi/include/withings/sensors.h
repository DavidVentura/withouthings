/* HWA10 (ScanWatch 2) application firmware v3411: the sensors module's ABI.
 *
 * What each of these is, not where it is: abi/symbols.yaml holds the
 * addresses. Written by abi/migrate.py from the old single manifest and
 * hand-maintained since. */
#ifndef WITHINGS_SENSORS_H
#define WITHINGS_SENSORS_H

#include "withings/bat.h"

/* tables */
/* the MAX86173 measurement channels by name (PPG_AFIB, HR_LOW, HR_HIGH,
   INACTIVITY, PPG_AFIB_NIGHT, then the twenty LED/photodiode pairings and
   the four ambient ones), loaded once at 0x518f4; the word below the run is
   zero and the word above is not an address
   */
extern struct name_ptr max86173_channel_names[25];
/* OFF, BURST_AUTO_STOP, BURST_MANUAL_STOP, CONTINUOUS -- the measure modes
   "[ADXL367] Set mode %d" counts; loaded at 0x62320
   */
extern struct name_ptr adxl367_mode_names[4];

#endif
