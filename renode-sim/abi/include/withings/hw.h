/* HWA10 (ScanWatch 2) application firmware v3411: the hw module's ABI.
 *
 * What each of these is, not where it is: abi/symbols.yaml holds the
 * addresses. Written by abi/migrate.py from the old single manifest and
 * hand-maintained since. */
#ifndef WITHINGS_HW_H
#define WITHINGS_HW_H

/* acquire and release are the two ends of one bracket, and three of the seven
   drivers write them the same way: row 1's 0x5b400 (ads1115), row 2's 0x5e614
   (tmp117) and row 3's 0x50a8c (apds9306) are `ldr r0,<resource>; bl 0x56cc0`
   and nothing else, and their +0x18 twins 0x5b428, 0x5e644 and 0x50a9c are the
   same call into 0x56d24. 0x56cc0 takes the resource's semaphore at +8, raises
   the use count at +0x10 and calls the power-up hook when it becomes 1;
   0x56d24 gives the semaphore back, drops the count and logs "[PWR] Negative
   counter on %s." when it was already zero, which is the firmware's own word
   for which of the two is the release. Row 4 (crown) writes the same bracket
   through its own pair, 0x493d8 and 0x4940c.

   slot_1 and slot_2 keep an index for a name because no caller fixes what they
   are: row 2 points both at 0x5e488 with two different literals, row 1 points
   both at the same body, and rows 3 and 4 leave slot_2 NULL. */
struct i2c_device {
    void *bus;
    unsigned int addr;
    unsigned int unknown;
    void *acquire;
    void *slot_1;
    void *slot_2;
    void *release;
};

/* state points at the device's own byte: the two rows hold 0x2002507d and
   0x2002507e, consecutive bytes of one .bss item, which is what the relink's
   RAM relocation at +0xc needs a pointer field for.
   */
struct spi_device {
    void *bus;
    unsigned int cs_pin;
    unsigned int mode;
    unsigned char *state;
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
