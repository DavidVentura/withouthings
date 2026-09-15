#!/bin/sh
# Build objdump-style listings of the app and the MBR+SoftDevice into out/ (git-ignored).
# Addresses are absolute flash addresses, so they match symbols.txt and the hooks.
set -e
cd "$(dirname "$0")"
mkdir -p out
llvm-objcopy -I binary -O elf32-littlearm --rename-section .data=.text,alloc,load,readonly,code appl.bin out/appl.elf
llvm-objdump -d --triple=thumbv7em-none-eabi --mattr=+vfp4 --adjust-vma=0x27000 out/appl.elf | sed 's/<_binary_appl_bin_start+[^>]*>//' > out/appl.dis
llvm-objcopy -I binary -O elf32-littlearm --rename-section .data=.text,alloc,load,readonly,code flash.bin out/flash.elf
llvm-objdump -d --triple=thumbv7em-none-eabi --start-address=0x0 --stop-address=0x27000 out/flash.elf | sed 's/<_binary_flash_bin_start+[^>]*>//' > out/sd.dis
wc -l out/appl.dis out/sd.dis
