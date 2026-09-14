#!/usr/bin/env python3
# Rebuild appl/bl/sd and the assembled internal-flash image from the firmware package.
import struct, pathlib

HERE = pathlib.Path(__file__).resolve().parent
PKG = HERE.parent / "hwa10_3411_Tf4fD4.bin"

data = PKG.read_bytes()
appl = data[0x44:0x44 + 0xca17c]        # VA 0x27000
bl = data[0xca1c0:0xca1c0 + 0x4000]     # VA 0xfc000
sd = data[0xce1c0:0xce1c0 + 0x26598]    # MBR @0 + SoftDevice (S140 7.3.0) @0x1000

(HERE / "appl.bin").write_bytes(appl)
(HERE / "bl.bin").write_bytes(bl)
(HERE / "sd.bin").write_bytes(sd)

flash = bytearray(b"\xff" * 0x100000)   # 1 MiB internal flash, erased
flash[0x0:len(sd)] = sd
flash[0x27000:0x27000 + len(appl)] = appl
flash[0xfc000:0xfc000 + len(bl)] = bl
(HERE / "flash.bin").write_bytes(flash)

reset = lambda off: struct.unpack('<I', flash[off + 4:off + 8])[0]
print(f"appl {len(appl):#x}  bl {len(bl):#x}  sd {len(sd):#x}")
print(f"reset vectors: MBR={reset(0):#x} SD={reset(0x1000):#x} appl={reset(0x27000):#x} bl={reset(0xfc000):#x}")
