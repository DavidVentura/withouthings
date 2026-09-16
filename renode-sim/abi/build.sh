#!/bin/sh
# Generate the ABI header/linker script, build the overlay, and write the
# patched image. Outputs land in out/ (git-ignored).
set -e
cd "$(dirname "$0")"
python3 gen.py
clang --target=thumbv7em-none-eabihf -mcpu=cortex-m4 -mfpu=fpv4-sp-d16 \
      -Os -ffreestanding -fno-builtin -fno-common -Wall -Wextra \
      -I out -c overlay.c -o out/overlay.o
ld.lld -T out/hwa10.ld -e overlay_battery_hook -o out/overlay.elf out/overlay.o
llvm-objcopy -O binary --only-section=.overlay_text out/overlay.elf out/overlay.bin
python3 patch.py
