/* Every module of the HWA10 application ABI.
 *
 * Written by abi/migrate.py and hand-maintained since. A consumer that wants
 * one module includes it directly; this is for the generated sources, which
 * cut across all of them. */
#ifndef WITHINGS_ALL_H
#define WITHINGS_ALL_H

#include "withings/bat.h"
#include "withings/ble.h"
#include "withings/boot.h"
#include "withings/crown.h"
#include "withings/dblib.h"
#include "withings/stores.h"
#include "withings/ecg.h"
#include "withings/flash.h"
#include "withings/freertos.h"
#include "withings/hands.h"
#include "withings/hr.h"
#include "withings/hw.h"
#include "withings/misc.h"
#include "withings/sensors.h"
#include "withings/sensors_sync.h"
#include "withings/shell.h"
#include "withings/wlog.h"
#include "withings/wpp.h"
#include "withings/wpp_objects.h"
#include "withings/wui.h"

#endif
