/* HWA10 (ScanWatch 2) application firmware v3411: the dblib module's ABI.
 *
 * What each of these is, not where it is: abi/symbols.yaml holds the
 * addresses. Written by abi/migrate.py from the old single manifest and
 * hand-maintained since. */
#ifndef WITHINGS_DBLIB_H
#define WITHINGS_DBLIB_H

/* the information-element id dblib keys on. The API takes it as a u16
   (0x47b00 stores it with strh).
   */
enum dblib_ie {
    /* perso shell table row 0xb41ec {"mac", 1, 0x5b011}; also named in the
       erase help line at 0xdda48; reached through dblib_get 0x48294 from
       0x36eb4, 0x592d4, 0x59474; entry length 0x12.
       */
    DBLIB_IE_MACADDRESS = 0x1,
    /* perso shell table row 0xb4210 {"wstarget", 2, 0x9b9fb}; reached
       through the perso shell table only.
       */
    DBLIB_IE_WSTARGET = 0x2,
    /* perso shell table row 0xb4204 {"secret", 3, 0x5af39}; also named in
       the erase help line at 0xdda48; reached through dblib_get 0x48294, the
       setter at 0x480f0 from 0x5af38, 0x93256, 0x9326c; entry length 0x11.
       */
    DBLIB_IE_SECRET = 0x3,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x5e0d4, 0x5e210, 0x5e2f8, 0x5e3e0; entry length 0xc.
       */
    DBLIB_IE_004 = 0x4,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x510f4, 0x51170; entry length 0x6.
       */
    DBLIB_IE_007 = 0x7,
    /* perso shell table row 0xb421c {"mfgid", 0xb, 0x5a581}; reached through
       dblib_get 0x48294 from 0x36ed8; entry length 0x4.
       */
    DBLIB_IE_MFGID = 0xb,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc, the
       setter at 0x47f58 from 0x5dfe4, 0x5e024, 0x5e18c; entry length 0x4.
       */
    DBLIB_IE_00F = 0xf,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x4c8a0, 0x4c908; entry length 0x4.
       */
    DBLIB_IE_011 = 0x11,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47f58 from
       0x2e1d8, 0x5e7e0.
       */
    DBLIB_IE_013 = 0x13,
    /* reached through the setter at 0x47ebc from 0x40274; entry length 0x8. */
    DBLIB_IE_019 = 0x19,
    /* perso shell table row 0xb4234 {"bank_recover_cnt", 0x32, 0x5a52d};
       reached through the perso shell table only.
       */
    DBLIB_IE_BANK_RECOVER_CNT = 0x32,
    /* the shell's `adxl calibrate x|y|z` branch (strings
       0x3cc44/0x3cc4c..0x3cc54) reads and writes it at 0x3c9dc/0x3ca04;
       reached through dblib_get 0x48294, the setter at 0x48200 from 0x3c6b8;
       entry length 0x12.
       */
    DBLIB_IE_ACCELERO_CALIB_MATRIX = 0x43,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc, the
       setter at 0x47e58 from 0x4bcb0, 0x6037c, 0x603d8, 0x6060c, 0x92170;
       entry length 0x58.
       */
    DBLIB_IE_048 = 0x48,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x60a54, 0x60a88; entry length 0xc.
       */
    DBLIB_IE_04A = 0x4a,
    /* perso shell table row 0xb4228 {"factory_fw", 0x4c, 0x5aa2d}; reached
       through the perso shell table only.
       */
    DBLIB_IE_FACTORY_FW = 0x4c,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x94700; entry length 0x104.
       */
    DBLIB_IE_055 = 0x55,
    /* the debug-dump parts mask, cached at 0x20019234 (FIRMWARE.md); written
       at 0x48f68; reached through dblib_get_first 0x9776e, the setter at
       0x47ebc from 0x48f30, 0x48f68; entry length 0x4.
       */
    DBLIB_IE_DEBUG_MASK = 0x5b,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x33028, 0x330f8; entry length 0x180.
       */
    DBLIB_IE_068 = 0x68,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x607e4, 0x60854; entry length 0x4.
       */
    DBLIB_IE_06F = 0x6f,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x60824, 0x60854; entry length 0x4.
       */
    DBLIB_IE_070 = 0x70,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x3d450, 0x3d65c, 0x9444a, 0x944e2; entry length 0x50.
       */
    DBLIB_IE_075 = 0x75,
    /* the shell's quartz calibration command 0x34654 reads it under `get`
       and writes it under `set`; reached through dblib_get 0x48294, the
       setter at 0x48200 from 0x34654; entry length 0x4.
       */
    DBLIB_IE_QUARTZ_MILLIHZ = 0x76,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc, the
       setter at 0x47e58 from 0x590c4, 0x9b82e; entry length 0x16c.
       */
    DBLIB_IE_077 = 0x77,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x395dc, 0x3b5a8, 0x60528, 0x60738, 0x6c59c, 0x6c60c; entry length
       0x21.
       */
    DBLIB_IE_079 = 0x79,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc, the
       setter at 0x47e58 from 0x2e1d8, 0x2e50c, 0x9259c; entry length 0x14.
       */
    DBLIB_IE_07C = 0x7c,
    /* reached through dblib_get 0x48294, the setter at 0x48200 from 0x56788,
       0x9af6a; entry length 0x800.
       */
    DBLIB_IE_07D = 0x7d,
    /* the calibration phase of each step motor: one byte per motor, and the
       two functions that touch it are step_motor_load_cal_ph and
       step_motor_save_cal_ph, which log "[STEP_MOTOR] load cal ph." and
       "[STEP_MOTOR] save cal ph.". Reached through dblib_get_first 0x9776e,
       the setter at 0x47ebc from 0x5cc38, 0x5cd10; entry length 0x8.
       */
    DBLIB_IE_HANDS_CAL_PHASE = 0x81,
    /* reached through the setter at 0x47ebc from 0x44784; entry length 0x10. */
    DBLIB_IE_086 = 0x86,
    /* reached through the setter at 0x47ebc from 0x3baf0; entry length 0x1. */
    DBLIB_IE_088 = 0x88,
    /* perso shell table row 0xb41f8 {"pubkey", 0x98, 0x5aa8d}; reached
       through the perso shell table only.
       */
    DBLIB_IE_PUBKEY = 0x98,
    /* reached through dblib_get 0x48294, the setter at 0x48200, the setter
       at 0x480f0 from 0x5af38, 0x9326c; entry length 0x8.
       */
    DBLIB_IE_099 = 0x99,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x5d604, 0x5d6e8; entry length 0x8.
       */
    DBLIB_IE_09A = 0x9a,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x5d604, 0x5d69c; entry length 0x8.
       */
    DBLIB_IE_09B = 0x9b,
    /* reached through the setter at 0x48200 from 0x6d0e0; entry length 0x12. */
    DBLIB_IE_09D = 0x9d,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x4bcb0, 0x4bd18; entry length 0x1.
       */
    DBLIB_IE_0A2 = 0xa2,
    /* reached through dblib_get 0x48294, the setter at 0x48200, the setter
       at 0x4814c from 0x53b78, 0x53c08, 0x53db4, 0x9abba; entry length 0x20.
       */
    DBLIB_IE_0A5 = 0xa5,
    /* reached through dblib_query_init 0x47b00, the setter at 0x47f58 from
       0x6ea20, 0x9d684, 0x9d6be, 0x9d6d0; entry length 0x14.
       */
    DBLIB_IE_0A7 = 0xa7,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc, the
       setter at 0x47f58 from 0x2fc28, 0x3119c, 0x407c4; entry length 0x4.
       */
    DBLIB_IE_0A8 = 0xa8,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x3e808, 0x3f060, 0x3f1e4, 0x3fb48; entry length 0x8.
       */
    DBLIB_IE_0AB = 0xab,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x40eb4, 0x41018, 0x41088, 0x948d6; entry length 0x6e, 0x226.
       */
    DBLIB_IE_0AF = 0xaf,
    /* perso shell table row 0xb4240 {"battery_cal", 0xb5, 0x5a9b9}; reached
       through dblib_get 0x48294 from 0x40014; entry length 0x8.
       */
    DBLIB_IE_BATTERY_CAL = 0xb5,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x60a54, 0x60a88; entry length 0x1.
       */
    DBLIB_IE_0B6 = 0xb6,
    /* reached through dblib_get 0x48294, the setter at 0x48200 from 0x46b78,
       0x46ba4; entry length 0x4.
       */
    DBLIB_IE_0B8 = 0xb8,
    /* which hands are fitted: two bits per motor, set by
       step_motor_probe_coils when the winding continuity check passes.
       Reached through the setter at 0x47ebc from 0x9bef4; entry length 0x4.
       */
    DBLIB_IE_HANDS_PRESENT = 0xb9,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc, the
       setter at 0x47e58 from 0x3e808, 0x3f060, 0x3f1e4; entry length 0x6.
       */
    DBLIB_IE_0BA = 0xba,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x3051c, 0x340f8, 0x92f32; entry length 0x2.
       */
    DBLIB_IE_0BB = 0xbb,
    /* reached through dblib_get_first 0x9776e from 0x2e1d8. */
    DBLIB_IE_0BC = 0xbc,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc, the
       setter at 0x47e58 from 0x2e50c, 0x3119c, 0x9259c; entry length 0x18.
       */
    DBLIB_IE_0C0 = 0xc0,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x4be68, 0x4be9c; entry length 0x4.
       */
    DBLIB_IE_0C1 = 0xc1,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x3b574, 0x93ce6, 0x93d10, 0x93d4a; entry length 0x28.
       */
    DBLIB_IE_0CF = 0xcf,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x4ea68, 0x4eac0; entry length 0x1.
       */
    DBLIB_IE_0E2 = 0xe2,
    /* perso shell table row 0xb424c {"part_id", 0xe3, 0x5a87d}; reached
       through dblib_query_init 0x47b00 from 0x5a87c; entry length 0x16.
       */
    DBLIB_IE_PART_ID = 0xe3,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x69958; entry length 0x1.
       */
    DBLIB_IE_0E4 = 0xe4,
    /* reached through the setter at 0x47ebc from 0x2e1d8; entry length 0x8. */
    DBLIB_IE_0E6 = 0xe6,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x3cec0, 0x82a58, 0x923b0, 0x94480; entry length 0x1.
       */
    DBLIB_IE_0E8 = 0xe8,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x50abc, 0x50ebc; entry length 0x2.
       */
    DBLIB_IE_0EB = 0xeb,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x69464, 0x6976c; entry length 0x4.
       */
    DBLIB_IE_0ED = 0xed,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x36f58, 0x36fb4, 0x932a2, 0x932d2; entry length 0xc.
       */
    DBLIB_IE_0F6 = 0xf6,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x55704, 0x55788; entry length 0x1.
       */
    DBLIB_IE_0FD = 0xfd,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x55894; entry length 0x2.
       */
    DBLIB_IE_101 = 0x101,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x69414, 0x69464; entry length 0x48.
       */
    DBLIB_IE_102 = 0x102,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x658bc, 0x658e8, 0x6593c; entry length 0x4.
       */
    DBLIB_IE_103 = 0x103,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x4cdd4, 0x4ce0c; entry length 0x1.
       */
    DBLIB_IE_104 = 0x104,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x576d4, 0x57800; entry length 0xc.
       */
    DBLIB_IE_106 = 0x106,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x4c300, 0x4c32c, 0x4c360; entry length 0x4.
       */
    DBLIB_IE_107 = 0x107,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x378f4, 0x37a58.
       */
    DBLIB_IE_108 = 0x108,
    /* reached through dblib_query_init 0x47b00 from 0x944a0, 0x94530,
       0x9459a; entry length 0x28.
       */
    DBLIB_IE_112 = 0x112,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x2e50c, 0xa4fa0.
       */
    DBLIB_IE_11B = 0x11b,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x4a068, 0x4ac90, 0x61ea8, 0x6cf94, 0x6d188, 0x86d38; entry length
       0x1.
       */
    DBLIB_IE_11F = 0x11f,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x4b168, 0x4b7d0, 0x6cf48, 0x6d134, 0x85090, 0x857e4, 0x861d4,
       0x9c0e6; entry length 0x1.
       */
    DBLIB_IE_120 = 0x120,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x4ddc0, 0x4ddec, 0x4de24; entry length 0x4.
       */
    DBLIB_IE_124 = 0x124,
    /* perso shell table row 0xb4264 {"crt", 0x12b, 0x5abbd}; 0x974fc reads
       it as the client certificate mbedTLS is handed; reached through
       dblib_get 0x48294 from 0x974fc; entry length 0x202.
       */
    DBLIB_IE_TLS_CLIENT_CERT = 0x12b,
    /* named in the erase help line at 0xdda48; the perso row 0xb4258 calls
       it "csr", but 0x974fc and 0x6d50c read 0x40 bytes out of it and hand
       them to ed25519_sign as the key; reached through dblib_get 0x48294
       from 0x5abbc, 0x6d50c, 0x974fc; entry length 0x40.
       */
    DBLIB_IE_TLS_CLIENT_KEYS = 0x12c,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x4d370, 0x980ba; entry length 0x3.
       */
    DBLIB_IE_130 = 0x130,
    /* reached through the setter at 0x47ebc from 0x3c39c; entry length 0x8. */
    DBLIB_IE_131 = 0x131,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x982da; entry length 0x4.
       */
    DBLIB_IE_132 = 0x132,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x37004, 0x37028; entry length 0x4.
       */
    DBLIB_IE_133 = 0x133,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x59a44, 0x9b926; entry length 0x1.
       */
    DBLIB_IE_136 = 0x136,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x6ae78, 0x6aeb8; entry length 0x6.
       */
    DBLIB_IE_137 = 0x137,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x4bef8, 0x4bf68; entry length 0x140.
       */
    DBLIB_IE_13B = 0x13b,
    /* wpps_build_sni 0x553b4 builds the TLS SNI from it; reached through
       dblib_get_first 0x9776e, dblib_set 0x47df0 from 0x553b4, 0x55454;
       entry length 0x50.
       */
    DBLIB_IE_WPPS_REDIRECT = 0x141,
    /* reached through dblib_get_first 0x9776e from 0x9af06. */
    DBLIB_IE_142 = 0x142,
    /* reached through dblib_query_init 0x47b00 from 0x5e850, 0x5e9a4; entry
       length 0x34.
       */
    DBLIB_IE_146 = 0x146,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x51554, 0x516d8; entry length 0x28.
       */
    DBLIB_IE_147 = 0x147,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x6adfc, 0x6ae38; entry length 0x1.
       */
    DBLIB_IE_148 = 0x148,
    /* reached through dblib_query_init 0x47b00 from 0x9ac8c, 0x9acda,
       0x9ad18, 0x9ad72; entry length 0xc.
       */
    DBLIB_IE_149 = 0x149,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x5ee94, 0x5eef4; entry length 0x5.
       */
    DBLIB_IE_14D = 0x14d,
    /* reached through dblib_get_first 0x9776e, dblib_set 0x47df0, the setter
       at 0x47ebc from 0x64568, 0x64658, 0x647ac, 0x647f4; entry length 0x14.
       */
    DBLIB_IE_152 = 0x152,
    /* the only function that touches it, 0x7fb24, logs "[UI]
       DBLIB_IE_MOVE_HANDS not found" (0xe6854); reached through
       dblib_get_first 0x9776e, the setter at 0x47ebc from 0x6d038, 0x6d1ec,
       0x7fb24, 0x7fcac; entry length 0x1.
       */
    DBLIB_IE_MOVE_HANDS = 0x156,
    /* reached through dblib_query_init 0x47b00 from 0x53e9c, 0x9add6,
       0x9ae22; entry length 0x2c.
       */
    DBLIB_IE_15B = 0x15b,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc, the
       setter at 0x47f58 from 0x30af8, 0x30c08; entry length 0x2.
       */
    DBLIB_IE_164 = 0x164,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47f58 from
       0x2e1d8, 0x6bb24.
       */
    DBLIB_IE_167 = 0x167,
    /* reached through dblib_get 0x48294, the setter at 0x47f58, the setter
       at 0x48200, the setter at 0x480f0 from 0x4d728, 0x4d7a4, 0x4d860;
       entry length 0x1.
       */
    DBLIB_IE_16A = 0x16a,
    /* reached through dblib_get 0x48294, the setter at 0x48200 from 0x67a20,
       0x67a70, 0x9cc60; entry length 0x2.
       */
    DBLIB_IE_16E = 0x16e,
    /* reached through dblib_get 0x48294, the setter at 0x48200 from 0x49184,
       0x491c0; entry length 0x4.
       */
    DBLIB_IE_16F = 0x16f,
    /* reached through dblib_query_init 0x47b00, the setter at 0x47f58 from
       0x46ccc, 0x470dc, 0x97548, 0x97570; entry length 0x1c.
       */
    DBLIB_IE_170 = 0x170,
    /* reached through dblib_query_init 0x47b00, the setter at 0x47f58 from
       0x9ca18, 0x9ca5e; entry length 0x24.
       */
    DBLIB_IE_171 = 0x171,
    /* reached through dblib_query_init 0x47b00, the setter at 0x47f58 from
       0x9ca18, 0x9ca5e; entry length 0xc.
       */
    DBLIB_IE_172 = 0x172,
    /* reached through dblib_query_init 0x47b00, the setter at 0x47f58 from
       0x9ca18, 0x9ca5e; entry length 0x40.
       */
    DBLIB_IE_173 = 0x173,
    /* reached through dblib_query_init 0x47b00, the setter at 0x47f58 from
       0x46ec8, 0x47150, 0x47228; entry length 0x4.
       */
    DBLIB_IE_174 = 0x174,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x7fdd4, 0xa5502; entry length 0x1.
       */
    DBLIB_IE_177 = 0x177,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x4414c, 0x44178, 0x441ac; entry length 0x4.
       */
    DBLIB_IE_178 = 0x178,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x5ff04; entry length 0x70.
       */
    DBLIB_IE_179 = 0x179,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc, the
       setter at 0x47f58 from 0x2e1d8, 0x2f05c, 0x2f5fc, 0x613a0; entry
       length 0x34.
       */
    DBLIB_IE_17C = 0x17c,
    /* reached through dblib_get 0x48294, dblib_get_first 0x9776e, the setter
       at 0x47ebc, the setter at 0x47f58, the setter at 0x48200, the setter
       at 0x480f0 from 0x4d8a8, 0x4da04, 0x4dabc; entry length 0x10.
       */
    DBLIB_IE_180 = 0x180,
    /* reached through dblib_count 0x97746, dblib_query_init 0x47b00 from
       0x5ec34, 0x6a384; entry length 0xc.
       */
    DBLIB_IE_181 = 0x181,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x31a88, 0x31ab0; entry length 0x2.
       */
    DBLIB_IE_182 = 0x182,
    /* reached through dblib_get_first 0x9776e, the setter at 0x47ebc from
       0x404b8, 0x4056c; entry length 0x28.
       */
    DBLIB_IE_18D = 0x18d,
};

/* one axis of DBLIB_IE_ACCELERO_CALIB_MATRIX; the shell's `adxl calibrate
   <axis>` branch hands 0x3ca9c the three fields of one group as (u16*, i16*,
   i16*)
   */
struct dblib_calib_axis {
    unsigned short gain;
    short offset_a;
    short offset_b;
};

/* the cursor dblib_query_init fills in: 0x47b00 stores ie, len, -4, 0 and 0
   at +0, +2, +4, +8 and +0xa, and 0x47b40 copies the whole thing with a
   three-word ldm/stm
   */
struct dblib_query {
    unsigned short ie;
    unsigned short len;
    int cursor;
    unsigned short seq;
    unsigned char flags;
    unsigned char pad;
};

/* DBLIB_IE 0x1: 0x36eb4 reads 0x12 bytes and strcpy()s a default over them
   on a miss, so the entry is a NUL-terminated string.
   */
struct dblib_ie_macaddress {
    char value[18];
};

/* DBLIB_IE 0x3: 0x93256 reads 0x11 bytes and zeroes byte 0 on a miss, so the
   entry is a NUL-terminated string.
   */
struct dblib_ie_secret {
    char value[17];
};

/* DBLIB_IE 0xb: 0x36ed8 reads 4 bytes into one word and returns it. */
struct dblib_ie_mfgid {
    unsigned int value;
};

/* DBLIB_IE 0x43: the shell's `adxl calibrate <axis>` branch at 0x3c9dc reads
   0x12 bytes and writes one of three identical six-byte groups at +0, +6 and
   +0xc.
   */
struct dblib_ie_accelero_calib_matrix {
    struct dblib_calib_axis axis[3];
};

/* DBLIB_IE 0x76: 0x34654 reads and writes 4 bytes through one word. */
struct dblib_ie_quartz_millihz {
    unsigned int value;
};

/* DBLIB_IE 0x99: 0x9326c reads 8 bytes and passes them whole to 0x59474. */
struct dblib_ie_session_nonce {
    unsigned char value[8];
};

/* DBLIB_IE 0xa5: 0x9abba copies the 0x20 bytes into two four-word arrays and
   the setter writes x[ch] and y[ch] for a channel ch it parses out of the
   request; 0x53b78 interpolates between (x[0],y[0]) and (x[1],y[1]).
   */
struct dblib_ie_mcu_temp_cal {
    unsigned int x[4];
    unsigned int y[4];
};

/* DBLIB_IE 0xa7: 0x6ea20 enumerates 0x14-byte entries and keeps the one with
   the smallest first word, which it logs as a timestamp.
   */
struct dblib_ie_event_with_time {
    int timestamp;
    unsigned char payload[16];
};

/* DBLIB_IE 0xb5: 0x40014 reads 8 bytes into two words and shifts each right
   by three; dblib.py reads the pair as 4193 mV against 985 ADC counts.
   */
struct dblib_ie_battery_cal {
    unsigned int a;
    unsigned int b;
};

/* DBLIB_IE 0xb8: 0x46b78 reads 4 bytes into one word and caches it behind a
   loaded flag.
   */
struct dblib_ie_cust {
    unsigned int value;
};

/* DBLIB_IE 0xe3: 0x5a87c enumerates 0x16-byte entries, strcmp()s the first
   16 bytes against the shell argument and logs the last six one byte at a
   time.
   */
struct dblib_ie_part_id {
    char name[16];
    unsigned char value[6];
};

/* DBLIB_IE 0x112: 0x944a0 enumerates 0x28-byte entries and matches the first
   byte against a slot index it refuses above 10.
   */
struct dblib_ie_alarm_slot {
    unsigned char slot;
    unsigned char payload[39];
};

/* DBLIB_IE 0x12b: 0x974fc allocates 0x202 bytes, reads the entry into them,
   then copies der_len bytes from offset 2 as the certificate.
   */
struct dblib_ie_tls_client_cert {
    unsigned short der_len;
    unsigned char der[512];
};

/* DBLIB_IE 0x12c: 0x6d50c reads 0x40 bytes and passes them to ed25519_sign
   as the key; 0x974fc reads the same 0x40 into the TLS context.
   */
struct dblib_ie_tls_client_keys {
    unsigned char key[64];
};

/* DBLIB_IE 0x141: 0x55454 writes 0x50 bytes and wpps_build_sni 0x553b4
   formats them with "%s.%s".
   */
struct dblib_ie_wpps_redirect {
    char value[80];
};

/* DBLIB_IE 0x146: 0x5e850 reads 0x34 bytes and copies all thirteen words out
   one at a time after matching the first against its argument.
   */
struct dblib_ie_thresholds {
    int value[13];
};

/* DBLIB_IE 0x149: 0x9ac8c enumerates 0xc-byte entries and matches the low
   byte of the first word against the counter id.
   */
struct dblib_ie_counter {
    unsigned int key;
    unsigned int a;
    unsigned int b;
};

/* DBLIB_IE 0x15b: 0x9add6 enumerates 0x2c-byte entries, matches byte 0
   against its argument and sums seven halfwords starting at offset 2.
   */
struct dblib_ie_reset_event {
    unsigned char kind;
    unsigned char pad;
    unsigned short count[7];
    unsigned char rest[28];
};

/* DBLIB_IE 0x16a: 0x4d728 reads one byte and falls back to 0x4a. */
struct dblib_ie_greenteg_sensitivity_bin {
    unsigned char value;
};

/* DBLIB_IE 0x16e: 0x67a20 reads 2 bytes and passes each as a signed byte to
   0x9cfd2.
   */
struct dblib_ie_ie_0x16e {
    signed char value[2];
};

/* DBLIB_IE 0x16f: 0x491c0 reads 4 bytes and splits them into two halfwords
   its caller names the digital crown scale factor.
   */
struct dblib_ie_crown_scale {
    unsigned short a;
    unsigned short b;
};

/* DBLIB_IE 0x170: 0x470dc enumerates 0x1c-byte entries into a word-indexed
   array (param_1 + i*7) and stops after three.
   */
struct dblib_ie_menstrual_cycle_info {
    unsigned int field[7];
};

/* DBLIB_IE 0x174: 0x46ec8 enumerates 4-byte entries and compares the value
   against a time window.
   */
struct dblib_ie_menstrual_cycle_unk {
    unsigned int timestamp;
};

/* DBLIB_IE 0x180: 0x4d8a8 builds the 0x10 bytes as byte 0 = 0x4a, two zero
   words and a float, memcmp()s them against the stored entry and writes on a
   difference.
   */
struct dblib_ie_greenteg_integration_factor {
    unsigned char sensor;
    unsigned char pad[3];
    unsigned int a;
    unsigned int b;
    float factor;
};

/* DBLIB_IE 0x181: 0x5ec34 enumerates 0xc-byte entries, sums the second word
   for the entries whose third word is inside a 24 h window.
   */
struct dblib_ie_inactivity_sample {
    unsigned int a;
    int minutes;
    unsigned int timestamp;
};

/* functions */
/* one information element into the caller's buffer. The body builds a
   dblib_query on its stack (strh r0 at +0, strh r2 at +2, -4 at +4, zeros at
   +8 and +0xa, 0x482a2..0x482ca) and hands it to the store walk at 0x975ca
   with the caller's buffer in r2; 2 when the store is not initialised
   (0x482ee), 0 on a hit.
   */
extern int dblib_get(unsigned short ie, void *buf, unsigned short len);
/* the blob setter: passes (ie, buf, len, 0) to the common writer 0x475c4,
   whose fifth argument is a value-kind tag the other setter wrappers vary
   (0x47e58 and 0x480f0 pass 1, 0x47f58 and 0x4814c pass 2).
   */
extern int dblib_set(unsigned short ie, const void *buf, unsigned short len);
/* opens a cursor over every entry stored under one id. Stores ie, len, -4, 0
   and 0 into the twelve-byte descriptor at 0x47b12..0x47b20.
   */
extern int dblib_query_init(struct dblib_query *q, unsigned short ie, unsigned short len);
/* the next entry the cursor reaches into buf, non-zero when the cursor runs
   out; every enumerating caller loops on `== 0`.
   */
extern int dblib_query_next(struct dblib_query *q, void *buf);
/* closes the cursor; 0x472ac under the same lock dblib_query_init takes. */
extern int dblib_query_end(struct dblib_query *q);
/* dblib_query_init, one dblib_query_next, dblib_query_end, returning the
   next's result; the whole body is those three calls.
   */
extern int dblib_get_first(unsigned short ie, void *buf, unsigned short len);
/* how many entries are stored under one id: a cursor opened with length 0
   and drained with a null buffer, counting the iterations.
   */
extern int dblib_count(unsigned short ie);

#endif
