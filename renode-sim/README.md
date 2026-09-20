# Renode simulation of the ScanWatch 2 (HWA10)

It simulates the main SoC (nRF52840) running the three images in flash

- Nordic's MBR (forwards interrupts and SVCs)
- S140 SoftDevice (Nordic's BLE stack and SoC services)
- The application

<img src="../screenshots/renode-sim.png" width="420" alt="Now">

All the main sensors are modelled

| device | bus / pins |
|---|---|
| MX25R6435F SPI flash | SPIM2 (0x40023000), CS P0.15 |
| ADXL367 accelerometer | SPIM2, CS P0.16, INT1 P0.27 |
| MAX86173 optical HR front end | SPIM1 (0x40004000), CS P0.26, INT P1.14 |
| SSD1320 OLED 144x98 | SPIM3 (0x4002F000), D/C P1.09 |
| BQ25180 charger | TWI0 (0x40003000) 0x6A |
| PAT9125 crown | TWI0 0x79, MOTION P1.13 |
| crown click | GPIO P1.11, active low |
| TMP117 skin temperature | TWI0 0x48 |
| ADS1115-class heat-flux ADC | TWI0 0x49 |
| APDS-9306 ambient light | TWI0 0x52 |
| MS5849 barometer | TWI0 0x77 |
| battery sense | SAADC AIN0 |
| hour, minute and tracker hands | PWM1 P1.2/P1.5/P1.6, PWM0 P1.1/P1.4/P1.7, PWM2 P1.3/P0.9/P0.10 |

There are some hacks to patch over Renode, i'd like to upstream them at some point.

The simulator can run faster than real time so it's usable for development; there's a BLE
to-socket adapter so that the simulator can talk to the rust code implementing the protocol.


## Dependencies

Yours to bring, both gitignored: the v3411 update package (`extract.py` cuts
the four images out of it) and a dump of your watch's SPI flash
(`external_flash.bin`, which holds the association secret).

On the machine: Renode 1.17 (`renode` on `PATH`, or `RENODE=`), python3 with
`pyyaml` and `pyelftools`, llvm-objdump/llvm-objcopy, and Rust for the
client. Everything else is fetched by two scripts into `~/ref-build`
(`ROOT=` to put it elsewhere): `abi/refbuild.sh` downloads the Arm GCC 13.2
toolchain, nRF5 SDK 17.1.0, newlib 4.3.0 and the chip's SVD and builds the
reference libraries the relink and the byte verdicts rest on;
`abi/ghidra/fetch.sh` downloads Ghidra 12.1.3 and makes its Python venv.
Nothing under `~/ref-build` is expected to exist by magic; a script made it
and the same script remakes it.

## How to run

Once, extract the 'partitions' from the package with `extract.py`

Before each run, resolve the memory patches and hooks the rig needs against the
image it is about to load, which writes `out/rig/`:

```bash
python3 abi/rig.py                                     # the stock flash.bin
python3 abi/rig.py --image out/flash-relinked.bin --symbols out/relink/relinked.elf
```

```bash
renode --disable-xwt --console -e '$ramfill="ff"' \
    -e "include @scripts/display-run.resc" < <(sleep 1000)
```

Interactive run:

```bash
renode --console --port 12345 -e "include @scripts/live.resc"
printf 'keys Tap "Enter"\n' | nc -q 1 localhost 12345
printf 'twi0.crown Rotate 1\n' | nc -q 1 localhost 12345
```

To validate the network protocol via TCP:

```bash
renode --disable-xwt --console -e "include @scripts/wpp-pipe.resc" < <(sleep 1000)
cargo run -p wpp-sim-client -- --secret-from-dump external_flash.bin   # once out/uart0.log shows "Add WPPS chars."
```

