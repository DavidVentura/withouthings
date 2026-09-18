/* HWA10 (ScanWatch 2) application firmware v3411: the ble module's ABI.
 *
 * What each of these is, not where it is: abi/symbols.yaml holds the
 * addresses. Written by abi/migrate.py from the old single manifest and
 * hand-maintained since. */
#ifndef WITHINGS_BLE_H
#define WITHINGS_BLE_H

#include "withings/bat.h"

struct nrf_nvic_state {
    unsigned int __irq_masks_0;
    unsigned int __irq_masks_1;
    unsigned int __cr_flag;
};

struct ble_gap_conn_params {
    unsigned short min_conn_interval;
    unsigned short max_conn_interval;
    unsigned short slave_latency;
    unsigned short conn_sup_timeout;
};

/* functions */
/* builds ble_gatts_hvx_params_t and issues svc 0xae via the thunk at 0x935b4 */
extern int sd_ble_gatts_hvx_send(const void *p_data, unsigned short len, unsigned char type);

/* globals */
extern unsigned char ble_evt_buffer[0xec];
/* 0xCAFEBABE once sd_softdevice_enable succeeded */
extern unsigned int sd_enabled_magic;
extern struct nrf_nvic_state nrf_nvic_state;

/* tables */
extern struct ble_gap_conn_params ble_conn_params_table[3];
/* BLE_STOP, BLE_SYNC_REQUEST, BLE_NORMAL, BLE_END; loaded by 0x40b38,
   0x40b94, 0x40bc4 and 0x40c2c, and bounded below by a non-pointer word
   */
extern struct name_ptr ble_state_names[4];

#endif
