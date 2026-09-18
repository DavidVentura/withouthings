/* HWA10 (ScanWatch 2) application firmware v3411: the hw module's ABI.
 *
 * What each of these is, not where it is: abi/symbols.yaml holds the
 * addresses. Written by abi/migrate.py from the old single manifest and
 * hand-maintained since. */
#ifndef WITHINGS_HW_H
#define WITHINGS_HW_H

struct i2c_device {
    void *bus;
    unsigned int addr;
    unsigned int unknown;
    void *fn0;
    void *fn1;
    void *fn2;
    void *fn3;
};

struct spi_device {
    void *bus;
    unsigned int cs_pin;
    unsigned int mode;
    unsigned int state;
    const char *name;
};

struct gpio_pin {
    unsigned short port;
    unsigned short pin;
};

/* tables */
extern struct i2c_device i2c_device_table[7];
/* The two SPIM2 devices, the external flash (cs_pin 0x000f0000) and the
   ADXL367 (0x00100000); cs_pin is the {u16 port, u16 pin} pair the gpio_pin
   table uses, so those read as P0.15 and P0.16. An earlier reading put the
   table at 0xb23c0 with three rows, which is wrong: 0xb23c0 and 0xb2404 are
   two more devices of the same shape (max86173 on SPIM1 CS P0.26 and the
   device on CS P0.25) but they are not contiguous with each other or with
   this pair, because each one's bus descriptor sits between them (0xb23c0's
   bus is 0xb23d4, which holds SPIM1's 0x40004000 at 0xb23f4, the address
   symbols.txt calls spi1_cfg). At stride 20 from 0xb23c0 the second row
   would start inside that bus descriptor and its name field would be
   0xb23ec, which is not a string.
   */
extern struct spi_device spi_device_table[2];
/* symbols.txt puts this at 0xbe458; that is 0x18 too low. 0xbe458..0xbe46f
   are the three ble_gap_conn_params_t (12,12,{0,15,30},600) FIRMWARE.md
   describes. The {port,pin} descriptors start at 0xbe470, which is what
   makes 0xbe478 = {1,13} (crown MOTION) and 0xbe4c8 = {1,11} (click).
   */
extern struct gpio_pin gpio_pin_table[24];

#endif
