/* HWA10 (ScanWatch 2) application firmware v3411: the sensors module's ABI.
 *
 * What each of these is, not where it is: abi/symbols.yaml holds the
 * addresses. Written by abi/migrate.py from the old single manifest and
 * hand-maintained since. */
#ifndef WITHINGS_SENSORS_H
#define WITHINGS_SENSORS_H

#include "withings/bat.h"

/* The body-temperature module's context, 80 bytes reached from one base by 17
   bodies. Every offset here is an access the image makes and every width is
   the width it makes it at; no offset is read at two widths and nothing
   dereferences any of them, so there is no pointer in it. The gaps are the
   bytes nothing reaches, and the end is the +80 the next addressed word
   establishes. The state byte the shell's refused `body_temperature start`
   gate reads is unknown_39 and the worn byte is unknown_61.
   */
struct body_temp_ctx {
    unsigned int unknown_0;
    unsigned int unknown_4;
    unsigned char unknown_8;
    unsigned char unknown_9;
    unsigned char pad_10[2];
    unsigned char unknown_12;
    unsigned char pad_13[3];
    unsigned int unknown_16;
    unsigned int unknown_20;
    unsigned int unknown_24;
    unsigned int unknown_28;
    unsigned char unknown_32;
    unsigned char unknown_33;
    unsigned char pad_34[2];
    unsigned char unknown_36;
    unsigned char unknown_37;
    unsigned char unknown_38;
    unsigned char unknown_39;
    unsigned char unknown_40;
    unsigned char pad_41[3];
    unsigned int unknown_44;
    unsigned int unknown_48;
    unsigned int unknown_52;
    unsigned int unknown_56;
    unsigned char unknown_60;
    unsigned char unknown_61;
    unsigned char unknown_62;
    unsigned char pad_63[5];
    unsigned int unknown_68;
    unsigned int unknown_72;
    unsigned int unknown_76;
};

extern struct body_temp_ctx body_temp_struct_20018e68;

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
