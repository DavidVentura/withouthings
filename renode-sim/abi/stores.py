#!/usr/bin/env python3
"""The storage layer: the SPI NOR driver, WFTL, and dblib's flash port.

    python3 abi/stores.py            # writes the store class of abi/symbols.yaml

The layers below the log tags never name themselves the way a view or a WPP
command does: WFTL's own strings name its errors, not its entry points. What
does name them is the shell, the call graph and the on-flash words -- `wftl
read <type> <index> <offset> <len>` is the prototype of 0x69608, and a body
that programs command 0x02 after command 0x06 is the page program whatever it
is called. So every row here carries the reading and the reading's checks: the
callees the body must still call and the globals it must still load. Re-running
the analysis and re-running this is what says the reading survived; a check
that fails is a refusal, not a stale name that wins silently.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import symbols as symmap  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
GHIDRA = os.path.join(HERE, "out", "ghidra")
CLASS = "store"

# address, name, kind, module, evidence; `calls` are callee addresses the body
# must still reach and `reads` are literal-pool values it must still load.
ROWS = [
    # --- the SPI NOR driver, under everything -------------------------------
    dict(address=0x9BA20, name="spi_flash_select", kind="function",
         module="FLASH", calls=[0x9BD74],
         evidence="the tCS spin (movs 0x80, subs 3, bpl at 0x9ba20..0x9ba24)"
                  " then the bus grab at 0x9bd74; every command body starts"
                  " with it"),
    dict(address=0x9BA2A, name="spi_flash_write_enable", kind="function",
         module="FLASH", calls=[0x9BA20, 0x9BE6E, 0x9BDA8],
         evidence="sends the one-byte command 0x06 (movs r1,#6 at 0x9ba34),"
                  " which is WREN, and waits the transfer out"),
    dict(address=0x9BA44, name="spi_flash_wait_wip_clear", kind="function",
         module="FLASH", calls=[0x9BA20, 0x9BE6E, 0x9BE7E, 0x9BDA8],
         evidence="sends command 0x05 (RDSR, movs r1,#5 at 0x9ba4e) and loops"
                  " at 0x9ba76 while bit 0 of the byte read back is set, which"
                  " is WIP; the r1 argument is the millisecond budget the chip"
                  " table gives for the operation"),
    dict(address=0x9BACA, name="spi_flash_read_status", kind="function",
         module="FLASH", calls=[0x9BA20, 0x9BE82, 0x9BDA8],
         evidence="command 0x05 (movs r3,#5 at 0x9bacc), one byte back"),
    dict(address=0x9BAF6, name="spi_flash_read_config", kind="function",
         module="FLASH", calls=[0x9BA20, 0x9BE82, 0x9BDA8],
         evidence="command 0x15 (movs r3,#0x15 at 0x9baf8), two bytes back"
                  " byte-swapped with rev16; 0x15 is Read Configuration"
                  " Register on the MX25R the detect table matches"),
    dict(address=0x9BB2C, name="spi_flash_send_cmd_addr", kind="function",
         module="FLASH", calls=[0x9BE76],
         evidence="builds {u8 cmd; u24 addr big-endian} on the stack (rev16 at"
                  " 0x9bb30 plus the lsr #16 byte at 0x9bb2e) and sends the"
                  " four bytes; the three-byte address is why the driver"
                  " cannot reach past 16 MB"),
    dict(address=0x9BB54, name="spi_flash_is_erased", kind="function",
         module="FLASH", calls=[0x5B748, 0x9BA20, 0x9BB2C, 0x9BE7A, 0x9BDA8,
                                0x5B6A4],
         evidence="reads the range with command 0x03 in 0x64-byte chunks and"
                  " returns 0 as soon as a word is not 0xffffffff (ldr, adds"
                  " #1, beq at 0x9bba8), 1 when the whole range is blank"),
    dict(address=0x9BBB6, name="spi_flash_program", kind="function",
         module="FLASH", calls=[0x5B748, 0x9BA2A, 0x9BA20, 0x9BB2C, 0x9BE76,
                                0x9BDA8, 0x9BA44, 0x5B6A4],
         evidence="(handle, buf, addr, len): write-enable, command 0x02"
                  " (movs r1,#2 at 0x9bbf2), data, wait WIP, repeated per page"
                  " because 0x9bbd0..0x9bbe4 clamps each pass to"
                  " 0x100 - (addr & 0xff)"),
    dict(address=0x5B6A4, name="spi_flash_unlock", kind="function",
         module="FLASH", calls=[0x73A90],
         evidence="gives back the mutex at *0x2001f8e8 with xQueueGenericSend;"
                  " the udf at 0x5b6aa is the assert that it exists",
         reads=[0x2001F8E8]),
    dict(address=0x5B748, name="spi_flash_lock", kind="function",
         module="FLASH", calls=[0x9E890],
         evidence="takes the same mutex with an infinite timeout (mov.w"
                  " r1,#0xffffffff at 0x5b750)", reads=[0x2001F8E8]),
    dict(address=0x5B6EC, name="spi_flash_deep_power_down", kind="function",
         module="FLASH", calls=[0x9BA20, 0x9BE6E, 0x9BDA8, 0x9BB4C],
         evidence="the one-byte command 0xb9 (movs r1,#0xb9 at 0x5b6f6),"
                  " then the tDP delay the chip row gives at +0"),
    dict(address=0x5B6BC, name="spi_flash_wake", kind="function",
         module="FLASH", calls=[0x9BB4C, 0x9BA20, 0x9BDA8],
         evidence="toggles chip select with no command at all and waits the"
                  " chip row's bytes at +1 and +2 around it, which is how a"
                  " NOR part leaves deep power down"),
    dict(address=0x5B718, name="spi_flash_release", kind="function",
         module="FLASH", calls=[0x5B6EC, 0x5C5FC],
         evidence="decrements the open count at +4 of the device and, on the"
                  " last release, powers the chip down and drops the SPI"
                  " device; -1 when the count is already zero"),
    dict(address=0x5B75C, name="spi_flash_erase_unit", kind="label",
         module="FLASH", calls=[0x5B748, 0x9BA2A, 0x9BA20, 0x9BB2C, 0x9BDA8,
                                0x9BA44, 0x5B6A4],
         evidence="command 0x20 when the erase shift at +0x10 of the device is"
                  " 0xc (4 KB sector) and 0xd8 when it is 0x10 or 0x12"
                  " (0x5b76e..0x5b772 against 0x5b7ac), then waits WIP for the"
                  " chip row's erase budget"),
    dict(address=0x5B7BC, name="spi_flash_erase_unit_if_dirty", kind="function",
         module="FLASH", calls=[0x9BB54, 0x5B75C],
         evidence="blank-checks the unit first and only erases when"
                  " spi_flash_is_erased says no; this is what keeps a rewrite"
                  " of an already-blank WFTL block off the erase counter"),
    dict(address=0x5B7E4, name="spi_flash_erase_chip", kind="function",
         module="FLASH", calls=[0x5B748, 0x9BA2A, 0x9BA20, 0x9BE6E, 0x9BDA8,
                                0x9BA44, 0x5B6A4],
         evidence="the one-byte command 0xc7 (movs r1,#0xc7 at 0x5b7f8) and a"
                  " WIP wait of the chip row's whole-chip seconds times 1000"),
    dict(address=0x5B884, name="spi_flash_erase_range", kind="function",
         module="FLASH", calls=[0x9BB54, 0x5B7BC, 0x5B748, 0x9BA2A, 0x9BA20,
                                0x9BB2C, 0x9BDA8, 0x9BA44, 0x5B6A4],
         evidence="(handle, addr, len): -1 unless addr is erase-unit aligned"
                  " and the range ends inside the chip (0x5b8ac..0x5b8c2),"
                  " then walks it taking the block erase (0x52 for the 32 KB"
                  " shift 0xf, 0xd8 for the 64 KB shift 0x10) where a whole"
                  " block is covered and blank and the sector erase otherwise"),
    dict(address=0x5B96C, name="spi_flash_total_size", kind="function",
         module="FLASH", evidence="1 << the chip-size shift at +0x12 of the"
                                  " device descriptor"),
    dict(address=0x5B97C, name="spi_flash_erase_unit_size", kind="function",
         module="FLASH", evidence="1 << the erase shift at +0x10 of the device"
                                  " descriptor, the same field"
                                  " spi_flash_erase_unit branches on"),

    # --- WFTL, the flash translation layer ----------------------------------
    dict(address=0x9D0CC, name="wftl_type_count", kind="function",
         module="WFTL",
         evidence="movs r0,#0x17; bx lr -- 23, and every entry point bounds"
                  " its type argument against it"),
    dict(address=0x9D07E, name="wftl_block_payload_size", kind="function",
         module="WFTL",
         evidence="movw r0,#0xff8; bx lr -- the 4096-byte block less its"
                  " 8-byte header, and the bound wftl_read and wftl_write put"
                  " on offset + len"),
    dict(address=0x9D042, name="wftl_block_read", kind="label",
         module="WFTL", calls=[0x6A1F0],
         evidence="(block, offset, buf, len) -> WFTL_ORIGIN + 8 + offset +"
                  " (block << 12); the +8 at 0x9d04a is the header the payload"
                  " starts after"),
    dict(address=0x9D05C, name="wftl_block_write", kind="label",
         module="WFTL", calls=[0x6A1BC],
         evidence="the same address arithmetic as wftl_block_read into the"
                  " port's program call"),
    dict(address=0x9D0D0, name="wftl_block_header_read", kind="function",
         module="WFTL", calls=[0x6A1F0],
         evidence="reads the 8 bytes at WFTL_ORIGIN + (block << 12) and"
                  " decodes struct wftl_block_header: 2 when the 6-bit type"
                  " is 0x3f (erased), 1 when the released bit or either index"
                  " sentinel 0x7fff says the block is not live, else 0 with"
                  " *out_type and *out_index filled; the shell's `wftl list`"
                  " prints BLOCK_STATE_EMPTY for 2 and BLOCK_STATE_CORRUPTED"
                  " for the rest"),
    dict(address=0x9D084, name="wftl_cursor_nearer", kind="function",
         module="WFTL",
         evidence="keeps whichever of two (index, block) cursors is the fewer"
                  " ring steps before the wanted index, the modulus being the"
                  " type's index count; wftl_find_block calls it once per"
                  " cached cursor before it walks"),
    dict(address=0x9D15C, name="wftl_block_erase", kind="function",
         module="WFTL", calls=[0x6A1F0, 0x6A224, 0x6A1BC],
         evidence="erases the block, then programs the header word back with"
                  " the 24-bit erase count -- the caller's when it supplies"
                  " one at 0x9d17c, the old one plus one at 0x9d1b6 -- and"
                  " clears bit 30 with the second program of 0xbfffffff at"
                  " 0x9d19e, which is what marks the block initialised"),
    dict(address=0x9D1BA, name="wftl_format", kind="function",
         module="WFTL", calls=[0x6A1F0, 0x9D15C, 0x69934],
         evidence="erases every block whose type is not 0x3f over the whole"
                  " 0x5c8 range, then resets all 23 per-type states"),
    dict(address=0x9D1FC, name="wftl_erase_addr_range", kind="function",
         module="WFTL", calls=[0x9D0D0, 0x9D15C],
         evidence="takes a {u32 addr; u32 len} descriptor, turns it into the"
                  " block numbers it covers by subtracting WFTL_ORIGIN and"
                  " shifting by 12, and erases each block that is not already"
                  " empty"),
    dict(address=0x69934, name="wftl_type_state_reset", kind="function",
         module="WFTL",
         evidence="writes the type's index count over all four (index, block)"
                  " cursors of its 18-byte state and 0x5c8 -- no block -- over"
                  " all four block halves"),
    dict(address=0x697EC, name="wftl_find_block", kind="function",
         module="WFTL", calls=[0x9D084, 0x6A1A4, 0x6A1F0, 0x6A1B0, 0x50CA8],
         evidence="(type, index) -> block, or 0x5c8. Rejects an index outside"
                  " the live ring [first_index, last_index], answers from the"
                  " last/write/read cursors when one of them holds the index,"
                  " and otherwise walks the block chain from the nearest"
                  " cursor following the next-block half of the header;"
                  " the miss is the \"[WFTL] Error : Should have found index"
                  " %d for type %d (%d)\" line"),
    dict(address=0x69564, name="wftl_write", kind="function",
         module="WFTL", calls=[0x9D0CC, 0x9E890, 0x69CA8, 0x73A90, 0x50CA8],
         evidence="the `wftl write <type> <index> <offset> <len>` shell"
                  " branch at 0x69236 calls it with exactly those four and the"
                  " buffer; 3 unless the type is below wftl_type_count, the"
                  " type's index count is non-zero, the index is below it and"
                  " offset + len is within wftl_block_payload_size",
         reads=[0x2000777A, 0x20020898]),
    dict(address=0x69608, name="wftl_read", kind="function",
         module="WFTL", calls=[0x9D0CC, 0x9E890, 0x69EC0, 0x73A90],
         evidence="the `wftl read <type> <index> <offset> <len>` shell branch"
                  " at 0x692ac, with the same four bounds checks as wftl_write"
                  " and no flags argument",
         reads=[0x2000777A, 0x20020898]),
    dict(address=0x69EC0, name="wftl_read_locked", kind="function",
         module="WFTL", calls=[0x697EC, 0x9D042],
         evidence="wftl_read under the mutex: finds the block, caches"
                  " (index, block) in the read cursor at +0xe/+0x10 of the"
                  " type state, and tail-calls wftl_block_read; 2 when the"
                  " index has no block", reads=[0x2000777A]),
    dict(address=0x69CA8, name="wftl_write_locked", kind="function",
         module="WFTL", calls=[0x697EC],
         evidence="wftl_write under the mutex; 5 is the not-found its caller"
                  " turns into the \"[WFTL] Type %d not requesting last block"
                  " + 1\" line", reads=[0x2000777A]),
    dict(address=0x69680, name="wftl_erase_index", kind="function",
         module="WFTL", calls=[0x9D0CC, 0x9E890, 0x69F00, 0x73A90, 0x50CA8],
         evidence="the `wftl erase <type> <index>` shell branch at 0x691aa;"
                  " the refusal it logs is \"[WFTL] Type %d not erasing first"
                  " block (%d instead of %d)\", which is the layer's rule that"
                  " only the head of the ring may go",
         reads=[0x2000777A, 0x20020898]),
    dict(address=0x69F00, name="wftl_erase_index_locked", kind="function",
         module="WFTL", calls=[0x697EC, 0x69934, 0x9D15C, 0x6A1F0],
         evidence="5 unless the index's block is the type's first block;"
                  " otherwise invalidates whichever cursors pointed at it,"
                  " erases it, and advances first_index by one modulo the"
                  " index count and first_block to the header's next block",
         reads=[0x2000777A]),
    dict(address=0x6970C, name="wftl_erase_type", kind="function",
         module="WFTL", calls=[0x9D0CC, 0x9E890, 0x69FB8, 0x73A90],
         evidence="the `wftl erase <type>` shell branch at 0x691b6, one"
                  " argument", reads=[0x20020898]),
    dict(address=0x69FB8, name="wftl_erase_type_locked", kind="function",
         module="WFTL", calls=[0x6A1F0, 0x9D15C, 0x69934],
         evidence="walks the chain from first_block erasing each block and"
                  " following the header's next-block half until it leaves the"
                  " 0x5c8 range, then resets the type state",
         reads=[0x2000777A]),
    dict(address=0x6979C, name="wftl_request_block", kind="function",
         module="WFTL", calls=[0x6A000],
         evidence="the only caller of wftl_reserve_blocks; the log its callee"
                  " reaches is \"[WFTL] Could not request block\""),
    dict(address=0x6A000, name="wftl_reserve_blocks", kind="function",
         module="WFTL", calls=[0x6A1A4, 0x6A1F0, 0x6A1B0, 0x697CC, 0x8F460,
                               0xA868E, 0xA86C2],
         evidence="scans all 0x5c8 blocks for erased ones and keeps the n with"
                  " the lowest erase counts, memmove-inserting each into a"
                  " sorted run; refuses above ten and logs \"[WFTL] Could not"
                  " reserve %u blocks\". This is the whole wear levelling the"
                  " layer has", reads=[0x20024DC0]),
    dict(address=0x697CC, name="wftl_release_reservation", kind="function",
         module="WFTL",
         evidence="zeroes the reserved count at +0x15 of wftl_reservation;"
                  " both the success and the failure path of"
                  " wftl_reserve_blocks end in it"),
    dict(address=0x694E0, name="wftl_init", kind="function",
         module="WFTL", calls=[0x69958],
         evidence="the `wftl init` shell branch at 0x69176, and what 0x2e1d8"
                  " calls at boot before any other store opens"),
    dict(address=0x69958, name="wftl_mount", kind="function",
         module="WFTL", calls=[0x6A1A4, 0x6A1F0, 0x9D15C, 0x69934, 0x6A1B0,
                               0x697CC, 0x8F460, 0x9776E, 0x47EBC],
         evidence="the scan wftl_init runs: reads every block header, drops"
                  " the uninitialised ones, counts the blocks per type against"
                  " the configured count -- \"[WFTL] Found %d / %d blocks for"
                  " type %d\" -- and rebuilds each type's first and last"
                  " cursors from the chain ends; a type that fails is reset and"
                  " its blocks erased", reads=[0x2000777A]),

    # --- circularfs, the append-only log WFTL carries -----------------------
    dict(address=0x4414C, name="cfs_factory_reset", kind="function",
         module="CFS",
         evidence="logs \"[%s] Factory reset\" with \"CIRCULARFS\" (0xd4f1a)"
                  " as the %s and writes the layer's dblib id 0x178"),
    dict(address=0x44178, name="cfs_post_update_check", kind="function",
         module="CFS",
         evidence="reads dblib id 0x178 and, when it is missing or not 1,"
                  " raises the flag at 0x2002528e with \"[CFS][WARN] Post"
                  " update erase needed\""),
    dict(address=0x441AC, name="cfs_post_update_mark", kind="function",
         module="CFS",
         evidence="the writer half of the 0x178 pair cfs_post_update_check"
                  " reads: stores 1 and clears 0x2002528e. Named for what it"
                  " writes and not for when, because the flag it clears reads"
                  " both ways"),
    dict(address=0x441D4, name="cfs_init", kind="function", module="CFS",
         calls=[0x6970C],
         evidence="initialises both cursors to {block 0, offset 4} (the strd"
                  " of 0x40000 at 0x441d4), erases the whole type on the force"
                  " path with \"[CFS][WARN] Init force erase type=%hu\", then"
                  " reads the 4-byte sequence number at offset 0 of every"
                  " block to find the oldest and the newest"),
    dict(address=0x442C0, name="cfs_next", kind="function", module="CFS",
         calls=[0x94E16],
         evidence="advances the read cursor to the next element that loads and"
                  " passes its CRC, skipping the rest with \"[CFS][WARN]"
                  " Corrupted el blk=%lu off=%lu (skipping)\"; -1 at the end"
                  " of the log"),
    dict(address=0x9523A, name="cfs_first", kind="function", module="CFS",
         calls=[0x94D0A, 0x94E16, 0x442C0],
         evidence="points the read cursor at {oldest block, offset 4} and"
                  " yields the element there, falling through to cfs_next"
                  " when that one does not load"),
    dict(address=0x94D0A, name="cfs_find_oldest_block", kind="function",
         module="CFS", calls=[0x94CD8],
         evidence="the block whose sequence number is the smallest of the"
                  " ring"),
    dict(address=0x94F00, name="cfs_block_seq_read", kind="function",
         module="CFS", calls=[0x94CD8],
         evidence="the u32 at offset 0 of a block, 0xffffffff when the read"
                  " fails -- which is also what an erased block holds"),
    dict(address=0x94CD8, name="cfs_block_read", kind="function", module="CFS",
         calls=[0x69608],
         evidence="bounds offset + len against wftl_block_payload_size and"
                  " tail-calls wftl_read with the context's type as the WFTL"
                  " type and the CFS block as the WFTL index"),
    dict(address=0x94E6C, name="cfs_block_write", kind="function", module="CFS",
         calls=[0x69564],
         evidence="the same bound into wftl_write"),
    dict(address=0x94EB6, name="cfs_recycle_block", kind="function",
         module="CFS", calls=[0x69680, 0x94E6C],
         evidence="erases the ring's next index and stamps ++last_seq at"
                  " offset 0 of it, pushing the read cursor off the block it"
                  " just dropped; this is where the log turns over"),
    dict(address=0x94CB0, name="cfs_cursor_wrap", kind="function",
         module="CFS",
         evidence="moves a (block, offset) cursor to {next block modulo the"
                  " ring, offset 4} once the offset passes"
                  " payload_size - 0x18, which is the layer's definition of a"
                  " full block"),
    dict(address=0x94C5C, name="cfs_locate_offset", kind="function",
         module="CFS",
         evidence="turns a logical offset inside an element into the"
                  " (block, offset) it lands at, taking the first chunk's"
                  " shorter run (payload - first_off - 0xc) off before"
                  " dividing by payload - 8; -1 asks for the append point"),
    dict(address=0x94D4A, name="cfs_element_crc", kind="function",
         module="CFS", calls=[0x94CD8],
         evidence="walks the element's chunks in reads of at most 0x80 bytes"
                  " through the CRC-32 the image carries at 0xb2614 (poly"
                  " 0xedb88320, init and final 0xffffffff)"),
    dict(address=0x94E16, name="cfs_element_load", kind="function",
         module="CFS", calls=[0x94CD8, 0x94D4A],
         evidence="reads the 12-byte head header, refuses a state other than"
                  " 0, fills the descriptor from its total length, and is"
                  " valid only when the recomputed CRC equals the word at"
                  " header+8"),
    dict(address=0x94F1E, name="cfs_element_begin", kind="function",
         module="CFS", calls=[0x94EB6, 0x94E6C],
         evidence="recycles the next block when the append cursor is past"
                  " payload_size - 0x18, writes an open head header and leaves"
                  " the descriptor at offset+0xc with length 0"),
    dict(address=0x94F90, name="cfs_element_write", kind="function",
         module="CFS", calls=[0x94E6C, 0x94EB6, 0x94E9A, 0x94C5C],
         evidence="appends (or overwrites at a logical offset) into the"
                  " element, opening a 4-byte continuation header in the next"
                  " block each time one fills, and grows the descriptor's"
                  " length"),
    dict(address=0x94E9A, name="cfs_seal_chunk_length", kind="function",
         module="CFS", calls=[0x94E6C],
         evidence="programs the u16 length of a header already on flash and"
                  " nothing else -- the flags byte it writes is 0xff, which"
                  " clears no bit. Named for the write and not for the intent,"
                  " because its two callers use it both to close a chunk and"
                  " to pad a block out"),
    dict(address=0x95102, name="cfs_element_commit", kind="function",
         module="CFS", calls=[0x94E9A, 0x94D4A, 0x94E6C],
         evidence="seals the last chunk, then programs the head header's total"
                  " length and CRC, which is what makes the element loadable"),
    dict(address=0x9516C, name="cfs_element_size", kind="function",
         module="CFS",
         evidence="the descriptor's length field"),
    dict(address=0x95170, name="cfs_element_read", kind="function",
         module="CFS", calls=[0x94CD8],
         evidence="walks the chunk headers from the element's first block,"
                  " skips the logical offset and copies out of the payload"),

    # --- WFTL's port on the SPI NOR driver ----------------------------------
    dict(address=0x6A1A4, name="wftl_port_open", kind="function",
         module="WFTL", calls=[0x5B830],
         evidence="flash_open(spi_flash_handle) and nothing else",
         reads=[0xB244C]),
    dict(address=0x6A1B0, name="wftl_port_close", kind="function",
         module="WFTL", calls=[0x9BC6A], reads=[0xB244C],
         evidence="the matching close of wftl_port_open"),
    dict(address=0x6A1BC, name="wftl_port_program", kind="function",
         module="WFTL", calls=[0x5B830, 0x9BBB6, 0x9BC6A], reads=[0xB244C],
         evidence="open, spi_flash_program(addr, buf, len), close; -1 when the"
                  " open fails"),
    dict(address=0x6A1F0, name="wftl_port_read", kind="function",
         module="WFTL", calls=[0x5B830, 0x9BC1C, 0x9BC6A], reads=[0xB244C],
         evidence="open, spi_flash_read(dest, addr, len), close"),
    dict(address=0x6A224, name="wftl_port_erase_block", kind="function",
         module="WFTL", calls=[0x5B830, 0x5B7BC, 0x9BC6A], reads=[0xB244C],
         evidence="open, spi_flash_erase_unit_if_dirty(addr), close -- the"
                  " erase unit and the WFTL block are both 4096 bytes, which"
                  " is the equality the \"[WFTL] Critical error! The flash have"
                  " a sector size of %i while WFTL expects a size of %i\" line"
                  " checks"),
    dict(address=0x6A250, name="wftl_port_erase_range", kind="function",
         module="WFTL", calls=[0x5B830, 0x5B884, 0x9BC6A], reads=[0xB244C],
         evidence="open, spi_flash_erase_range(addr, len), close"),
    dict(address=0x6A280, name="wftl_port_erase_unit_size", kind="function",
         module="WFTL", calls=[0x5B97C], reads=[0xB244C],
         evidence="spi_flash_erase_unit_size of the same handle"),
    dict(address=0x6A28C, name="wftl_post_update_cleanup", kind="function",
         module="WFTL", calls=[0x8F460, 0x32D38, 0x6A250],
         evidence="erases the whole WFTL region in twenty passes of 0x4a000"
                  " bytes from 0x238000 -- 20 * 0x4a000 is 0x5c8000, which is"
                  " the 0x5c8 blocks of 4096 -- logging \"[WFTL_UP] Cleanup in"
                  " progress (%d/%d) : %d -> %d\" for each"),

    # --- the flash cache, and the two caches built on it -------------------
    dict(address=0x69540, name="wftl_type_index_count", kind="function",
         module="WFTL",
         evidence="bounds the type against wftl_type_count and hands back the"
                  " index count at +0 of its state; the only reader outside"
                  " WFTL is the flash cache's shell test"),
    dict(address=0x4C594, name="flash_cache_init", kind="function",
         module="FLASH_CACHE", calls=[0x441D4, 0x73BC0],
         evidence="(ctx, wftl_type, nb_blocks, table, table_capacity, force):"
                  " logs \"[FLASH CACHE] Init ctx type %lu\", zeroes the RAM"
                  " lookup table sixteen bytes at a time, makes the context's"
                  " mutex and mounts the circularfs behind it"),
    dict(address=0x97D22, name="flash_cache_deinit", kind="function",
         module="FLASH_CACHE",
         evidence="destroys the mutex and memsets the whole 0x20-byte"
                  " context"),
    dict(address=0x4C390, name="flash_cache_insert", kind="function",
         module="FLASH_CACHE", calls=[0x94F1E, 0x94F90, 0x95102],
         evidence="(ctx, id, nseg, lens, bufs): refuses a duplicate with"
                  " \"[FLASH CACHE] Tried to insert already existing item"
                  " %lu\", then begins one circularfs element, writes the u32"
                  " id and every segment into it and commits it"),
    dict(address=0x4C470, name="flash_cache_read", kind="function",
         module="FLASH_CACHE", calls=[0x95170, 0x9516C],
         evidence="(ctx, id, out_len, buf, buf_size): the RAM table first,"
                  " then a walk comparing the element's leading u32 against"
                  " the id; the user length is the element's less the four"
                  " bytes of that id, and the miss logs \"[FLASH CACHE] Could"
                  " not read data\""),
    dict(address=0x4C53C, name="flash_cache_item_read", kind="function",
         module="FLASH_CACHE", calls=[0x95170],
         evidence="reads on through the handle flash_cache_foreach hands its"
                  " callback, clamping to what is left of the element"),
    dict(address=0x97C06, name="flash_cache_table_clear", kind="function",
         module="FLASH_CACHE",
         evidence="memsets the RAM table to zero; a zero id is the empty slot"
                  " the lookup skips"),
    dict(address=0x97C1E, name="flash_cache_table_lookup", kind="function",
         module="FLASH_CACHE",
         evidence="the linear scan of the RAM table for an id, copying out the"
                  " twelve-byte element descriptor it caches"),
    dict(address=0x97C4E, name="flash_cache_table_insert", kind="function",
         module="FLASH_CACHE",
         evidence="the ring insert at the table's next slot, replacing an"
                  " entry that already holds the id"),
    dict(address=0x97D1A, name="flash_cache_lock", kind="function",
         module="FLASH_CACHE", calls=[0x9E890],
         evidence="takes the context's own mutex with an infinite timeout"),
    dict(address=0x97D10, name="flash_cache_unlock", kind="function",
         module="FLASH_CACHE", calls=[0x73A90],
         evidence="gives it back"),
    dict(address=0x97C96, name="flash_cache_contains_ids", kind="function",
         module="FLASH_CACHE",
         evidence="walks the log once marking which of n ids it met; 1 only"
                  " when every one is present"),
    dict(address=0x97D40, name="flash_cache_contains_ids_locked",
         kind="label", module="FLASH_CACHE",
         evidence="flash_cache_contains_ids between the lock and the unlock"),
    dict(address=0x97D68, name="flash_cache_list_ids", kind="function",
         module="FLASH_CACHE", calls=[0x95170],
         evidence="walks the log reading the leading u32 of each element into"
                  " the caller's array"),
    dict(address=0x97DC8, name="flash_cache_walk", kind="function",
         module="FLASH_CACHE",
         evidence="builds the {context, descriptor, consumed} handle"
                  " flash_cache_item_read takes and calls the callback once"
                  " per element with its id and length"),
    dict(address=0x97E5A, name="flash_cache_format", kind="function",
         module="FLASH_CACHE", calls=[0x97C06, 0x441D4],
         evidence="clears the RAM table and remounts the circularfs with the"
                  " force-erase argument set"),
    dict(address=0x97E7C, name="flash_cache_preload_ids", kind="label",
         module="FLASH_CACHE",
         evidence="fills the RAM table from one walk for a given set of ids,"
                  " which is what keeps a glyph lookup off the flash"),
    dict(address=0x4C32C, name="flash_cache_post_update_check",
         kind="function", module="FLASH_CACHE",
         evidence="logs \"[FLASH CACHE] Post Update\" and raises the global"
                  " force-format flag at 0x2001a3ac when dblib id 0x107 is not"
                  " 2, which is the layer's format version"),
    dict(address=0x4C360, name="flash_cache_post_update_mark", kind="function",
         module="FLASH_CACHE",
         evidence="writes dblib id 0x107 = 2 and clears the force-format flag"
                  " its check raised"),
    dict(address=0x4D33C, name="glyph_cache_ctx", kind="function",
         module="GLYPH_CACHE",
         evidence="selector 0 is the context at 0x2001a554 and 1 the one at"
                  " 0x2001a574; anything else is NULL"),
    dict(address=0x4D3D4, name="glyph_cache_init", kind="function",
         module="GLYPH_CACHE", calls=[0x4C594],
         evidence="two flash_cache_init calls: WFTL type 6 over 2 blocks with"
                  " no RAM table, and WFTL type 7 over 3 blocks with a"
                  " twelve-entry table at 0x2001a494"),
    dict(address=0x980DE, name="glyph_cache_insert", kind="function",
         module="GLYPH_CACHE", calls=[0x4C390],
         evidence="refuses a width or height above 0x22 and inserts the glyph"
                  " as two segments, {u8 width; u8 height} and the"
                  " width * ((height + 7) >> 3) bitmap bytes"),
    dict(address=0x9812A, name="glyph_cache_get", kind="function",
         module="GLYPH_CACHE", calls=[0x4C470],
         evidence="reads the record back into the caller's buffer and fills"
                  " the in-RAM glyph with its width, height and a pointer two"
                  " bytes in"),
    dict(address=0x9815A, name="glyph_cache_contains_ids", kind="function",
         module="GLYPH_CACHE",
         evidence="the selector's context into flash_cache_contains_ids_locked"),
    dict(address=0x9816E, name="glyph_cache_format", kind="function",
         module="GLYPH_CACHE", calls=[0x97E5A],
         evidence="the selector's context into flash_cache_format"),
    dict(address=0x9817C, name="glyph_cache_preload_ids", kind="function",
         module="GLYPH_CACHE",
         evidence="the selector's context into flash_cache_preload_ids"),
    dict(address=0x4D370, name="glyph_cache_post_update_check",
         kind="function", module="GLYPH_CACHE", calls=[0x97E5A, 0x980BA],
         evidence="logs \"[GLYPH CACHE] Post Update\" and formats both glyph"
                  " contexts unless dblib id 0x130 reads back {0x22, 0x22,"
                  " font version}, the two 0x22 being the glyph dimension"
                  " ceiling glyph_cache_insert enforces"),
    dict(address=0x980BA, name="glyph_cache_write_version", kind="function",
         module="GLYPH_CACHE",
         evidence="writes dblib id 0x130 as {0x22, 0x22, the current font"
                  " version}"),
    dict(address=0x4D354, name="glyph_cache_factory_reset", kind="function",
         module="GLYPH_CACHE", calls=[0x980BA],
         evidence="the \"[%s] Factory reset\" line with \"GLYPH CACHE\" as the"
                  " %s, then the version write"),
    dict(address=0x6AF2C, name="workout_cache_init", kind="function",
         module="WORKOUT_CACHE", calls=[0x4C594],
         evidence="flash_cache_init of the context at 0x200208d4 over WFTL"
                  " type 0xa, one block, no RAM table"),
    dict(address=0x6AF48, name="workout_cache_store", kind="function",
         module="WORKOUT_CACHE", calls=[0x4C390],
         evidence="the key's top nibble is the record kind (0 the 0x24-byte"
                  " info, 1 the small icon bounded at 20x20, 2 the big icon"
                  " bounded at 34x34) and the rest is the workout id"),
    dict(address=0x6AFDC, name="workout_cache_get", kind="function",
         module="WORKOUT_CACHE", calls=[0x4C470],
         evidence="reads one key back as either the info bytes or a glyph,"
                  " by the same top-nibble kind workout_cache_store writes"),
    dict(address=0x6B034, name="workout_cache_list_ids", kind="function",
         module="WORKOUT_CACHE", calls=[0x97D68],
         evidence="flash_cache_list_ids filtered on the key's top nibble, so"
                  " it answers \"which workouts have an icon of this kind\""),
    dict(address=0x6B09C, name="workout_cache_format", kind="function",
         module="WORKOUT_CACHE", calls=[0x97E5A],
         evidence="flash_cache_format of the workout context"),
    dict(address=0x6AEB8, name="workout_cache_post_update_check",
         kind="function", module="WORKOUT_CACHE", calls=[0x97E5A, 0x6AE78],
         evidence="logs \"[WORKOUT CACHE] Post Update\" and formats the cache"
                  " unless dblib id 0x137 reads back {1, 0x20, 0x14, 0x14,"
                  " 0x22, 0x22} -- the format version and the four dimension"
                  " ceilings workout_cache_store enforces"),
    dict(address=0x6AE78, name="workout_cache_write_version", kind="function",
         module="WORKOUT_CACHE",
         evidence="writes those six bytes to dblib id 0x137"),
    dict(address=0x6AE9C, name="workout_cache_factory_reset", kind="function",
         module="WORKOUT_CACHE", calls=[0x6AE78],
         evidence="the \"[%s] Factory reset\" line with \"WORKOUT CACHE\" as"
                  " the %s, then the version write"),
    dict(address=0x6B0A8, name="workout_cache_wpp_reset", kind="function",
         module="WORKOUT_CACHE_WPP",
         evidence="zeroes the WPP staging state at 0x20006448 and the"
                  " 36-byte info buffer at 0x20006450"),
    dict(address=0x6B1B0, name="workout_cache_wpp_store_icon", kind="function",
         module="WORKOUT_CACHE_WPP", calls=[0x6AF48],
         evidence="the \"[WORKOUT CACHE WPP] [WPP] Store small icon\" and"
                  " \"Store big icon\" lines, which pick the 1 and 2 kind"
                  " nibbles workout_cache_store keys on"),
    dict(address=0x4CC08, name="spi_flash_region_crc32", kind="function",
         module="FLASH", calls=[0x5B830, 0x9BC1C, 0x9BC6A],
         evidence="CRC-32s a raw external-flash range in 0x200-byte reads."
                  " It is addressed in SPI-NOR bytes and not in WFTL blocks,"
                  " so it belongs to the driver and not to any store; its only"
                  " caller compares the answer against an expected CRC"),

    # --- the vasistas store, the measurement records the phone walks -------
    dict(address=0x657D0, name="vasistas_banks_init", kind="function",
         module="VASISTAS",
         evidence="fills the fourteen 0x18-byte bank descriptors at"
                  " 0x20020250 with their WFTL type and their ring length;"
                  " the pairs are (0,65) (1,6) (2,2) (0xb,3) (4,3) (3,47)"
                  " (5,20) (0x10,10) (0x11,4) (0x12,19) (0x13,28) (0x14,4)"
                  " (0x15,3) (0x16,2)"),
    dict(address=0x9C602, name="vasistas_bank_count", kind="function",
         module="VASISTAS",
         evidence="movs r0,#0xe; bx lr -- the fourteen banks"
                  " vasistas_banks_init writes"),
    dict(address=0x65864, name="vasistas_bank_reset", kind="function",
         module="VASISTAS",
         evidence="zeroes a bank's tail, head, write offset and last record"
                  " size and leaves its type and ring length, which is what"
                  " says those two are configuration and the rest is state"),
    dict(address=0x65884, name="vasistas_bank_for_type", kind="function",
         module="VASISTAS",
         evidence="the linear scan over the fourteen descriptors for the one"
                  " whose WFTL type byte matches; the miss is the \"vas: can't"
                  " get coords for bank\" line, so a bank is addressed by its"
                  " WFTL type and not by its ordinal"),
    dict(address=0x66524, name="vasistas_record_size", kind="function",
         module="VASISTAS",
         evidence="the tbb table at 0x66534, 63 entries indexed by the 6-bit"
                  " record type, giving the record's whole byte count; types"
                  " 0xf and 0x13 add the header (0x10 and 8) to the length the"
                  " record carries. The refusal above 0x3e is \"[VASISTAS]"
                  " Error: unknown vasistas size\""),
    dict(address=0x665B4, name="vasistas_append", kind="function",
         module="VASISTAS", calls=[0x9C9A4],
         evidence="writes the previous record's size into bits 6..13 of the"
                  " header (the bfi at 0x665c0 out of the bank's +0x10) and"
                  " the type's payload size into bits 14..21 with bit 22 set,"
                  " then wftl_writes the record and advances the bank's write"
                  " offset, taking the next ring index when the block fills"),
    dict(address=0x666E4, name="vasistas_size_from_header", kind="function",
         module="VASISTAS",
         evidence="the same total size taken out of the record's own bits"
                  " 14..21 instead of the table; vasistas_read_record computes"
                  " both and refuses the record when they differ, which is the"
                  " only integrity check the format has"),
    dict(address=0x6673C, name="vasistas_read_record", kind="function",
         module="VASISTAS", calls=[0x666E4, 0x66524],
         evidence="reads the 8-byte header first, agrees the two sizes, then"
                  " reads the whole record; -4 on the size disagreement at"
                  " 0x66788 and a type of 0x3f written back on a read error"),
    dict(address=0x9C9A4, name="vasistas_block_write_record", kind="function",
         module="VASISTAS", calls=[0x69564, 0x66524],
         evidence="wftl_write of vasistas_record_size bytes at the bank's"
                  " (index, offset)"),
    dict(address=0x9C9CA, name="vasistas_block_write_header", kind="function",
         module="VASISTAS", calls=[0x69564],
         evidence="the same write of 8 bytes only, which is how a header is"
                  " amended in place"),
    dict(address=0x9C9DE, name="vasistas_block_erase", kind="function",
         module="VASISTAS", calls=[0x69680],
         evidence="wftl_erase_index of the bank's head block"),
    dict(address=0x9C9E6, name="vasistas_bank_erase", kind="function",
         module="VASISTAS", calls=[0x6970C],
         evidence="wftl_erase_type of the bank's whole type"),
    dict(address=0x9C7A8, name="vasistas_advance_block", kind="function",
         module="VASISTAS", calls=[0x9C9DE],
         evidence="erases the next ring index and takes it as the new head,"
                  " pushing the tail one on when the head caught it; this is"
                  " where the oldest measurements go"),
    dict(address=0x9C7EC, name="vasistas_cursor_advance", kind="function",
         module="VASISTAS",
         evidence="adds n to a cursor's offset and takes the next ring index"
                  " at the block capacity"),
    dict(address=0x9C81C, name="vasistas_cursor_invalidate", kind="function",
         module="VASISTAS",
         evidence="zeroes the cursor and sets the record type to 0x3f, the"
                  " six-bit all-ones the format uses for no record"),
    dict(address=0x66B98, name="vasistas_bank_for_record", kind="function",
         module="VASISTAS",
         evidence="the tbb at 0x66bae maps record types 9..0x14 to the WFTL"
                  " type of the bank they belong in, and 0xff is the answer"
                  " every caller checks before it stores"),
    dict(address=0x65B38, name="vasistas_store", kind="function",
         module="VASISTAS", calls=[0x65884, 0x665B4],
         evidence="bank_for_type, then vasistas_append, then the first and"
                  " last timestamp globals; the \"[vasistas][%d][ bank %d ]\""
                  " line is its own"),
    dict(address=0x9C606, name="vasistas_store_enabled", kind="function",
         module="VASISTAS", calls=[0x65B38],
         evidence="the gate every sensor writer goes through: refuses the"
                  " 0xff bank vasistas_bank_for_record returns and tail-calls"
                  " vasistas_store"),
    dict(address=0x65B70, name="vasistas_store_if_initialised", kind="function",
         module="VASISTAS", calls=[0x65B38],
         evidence="the same store behind the initialised flag"),
    dict(address=0x659A4, name="vasistas_erase_all", kind="function",
         module="VASISTAS", calls=[0x6970C, 0x65864],
         evidence="the shell's `vasistas erase_all`: wftl_erase_type and a"
                  " reset for each of the fourteen banks"),
    dict(address=0x659E4, name="vasistas_init", kind="function",
         module="VASISTAS", calls=[0x657D0],
         evidence="the shell's `vasistas init`: rebuilds every bank's head and"
                  " tail by scanning its blocks and raises the initialised"
                  " flag the store checks"),
    dict(address=0x65970, name="vasistas_erase_bank", kind="function",
         module="VASISTAS", calls=[0x9C9E6, 0x65864],
         evidence="erases one WFTL type and resets every bank descriptor"
                  " carrying it"),
    dict(address=0x65C48, name="vasistas_write_bkp", kind="function",
         module="VASISTAS", calls=[0x65B38],
         evidence="builds the sixteen-byte record whose type field is 0xa"
                  " (movs r3,#0xa at 0x65c5e) and stores it in bank 2"),
    dict(address=0x9C41C, name="vasistas_walk_open", kind="label",
         module="VASISTAS",
         evidence="seeds a read cursor at the first record at or after a"
                  " timestamp, scanning forward from the bank's tail"),
    dict(address=0x9C832, name="vasistas_walk_next", kind="function",
         module="VASISTAS", calls=[0x6673C],
         evidence="reads at the cursor and, at the end of a block, takes the"
                  " next ring index and retries"),
    dict(address=0x9C8CA, name="vasistas_walk_prev", kind="function",
         module="VASISTAS", calls=[0x6673C],
         evidence="steps back by the previous-record size the header carries"
                  " in bits 6..13, which is the only reason a backwards walk"
                  " is possible in a format with no index"),
    dict(address=0x9C558, name="vasistas_walk_step", kind="function",
         module="VASISTAS", calls=[0x9C832, 0x9C7EC],
         evidence="one record, then the advance, then the timestamp stop"
                  " condition; -3 once the walk is past the requested range"),
    dict(address=0x9C62C, name="vasistas_count_from", kind="function",
         module="VASISTAS", calls=[0x9C558],
         evidence="walks to exhaustion returning the count the \"%d vasistas"
                  " in bank %d\" line prints"),
    dict(address=0x9C686, name="vasistas_count_bank", kind="function",
         module="VASISTAS", calls=[0x9C62C],
         evidence="the shell's `vasistas num <type>`: a cursor from timestamp"
                  " zero drained by vasistas_count_from"),
    dict(address=0x9C6DC, name="vasistas_read_last", kind="function",
         module="VASISTAS", calls=[0x65884],
         evidence="the newest record of one bank"),
    dict(address=0x9C746, name="vasistas_read_last_bkp", kind="function",
         module="VASISTAS",
         evidence="vasistas_read_last of bank 2 into sixteen bytes, the record"
                  " vasistas_write_bkp writes; the shell's `vasistas last bkp`"),

    # --- dblib's own port, which does not go through WFTL -------------------
    dict(address=0x48918, name="dblib_port_open", kind="function",
         module="DBLIB_PORT", calls=[0x5B830, 0x5E7A8], reads=[0xB244C],
         evidence="flash_open(spi_flash_handle) with the udf at 0x48928 as the"
                  " assert; the line it logs first is \"[DBLIB_PORT] can't open"
                  " external flash\""),
    dict(address=0x48934, name="dblib_port_close", kind="function",
         module="DBLIB_PORT", calls=[0x9BC6A], reads=[0xB244C],
         evidence="the matching close"),
    dict(address=0x48940, name="dblib_bank_erase", kind="function",
         module="DBLIB_PORT", calls=[0x5E7A8, 0x5B884], reads=[0xB244C],
         evidence="spi_flash_erase_range(bank->base, bank->erase_len) for a"
                  " bank whose medium byte at +0x14 is 0, then rewinds the"
                  " cursor at +0x10; the log is \"[DBLIB] erasebank bank %d\""),
    dict(address=0x4896C, name="dblib_bank_write", kind="function",
         module="DBLIB_PORT", calls=[0x5E7A8, 0x9BBB6], reads=[0xB244C],
         evidence="programs len bytes at bank->base + bank->cursor and"
                  " advances the cursor; 0 and the \"[DBLIB_PORT] w addr oor\""
                  " line when cursor + len would pass bank->size"),
    dict(address=0x489AC, name="dblib_bank_read", kind="function",
         module="DBLIB_PORT", calls=[0x5E7A8, 0xA8812, 0x9BC1C], reads=[0xB244C],
         evidence="reads len bytes from bank->base + bank->cursor, through"
                  " spi_flash_read for medium 0 and memcpy for medium 1, which"
                  " is how the same bank type serves the in-RAM copy; the"
                  " refusal is \"[DBLIB_PORT] r addr oor\""),
    dict(address=0x489F4, name="dblib_bank_copy", kind="function",
         module="DBLIB_PORT", calls=[0x5E7A8, 0x48940, 0x489AC, 0x4896C],
         evidence="erases the destination bank and moves the source bank into"
                  " it in 0x20-byte reads and writes, which is the compaction"
                  " behind \"[DBLIB] defaultbank old doublon ie id\""),
]


def load_analysis():
    with open(os.path.join(GHIDRA, "items.json")) as fh:
        items = json.load(fh)
    with open(os.path.join(GHIDRA, "references.json")) as fh:
        refs = json.load(fh)
    starts = dict((f["start"], f) for f in items["functions"])
    calls, pool = {}, {}
    for call in refs["calls"]:
        if call.get("function") is None:
            continue
        # A tail call is a jump out of the body, and half the driver's
        # wrappers end in one; the evidence is the callee either way.
        # Half these wrappers end in a tail call and the partition records
        # that as a jump, so the edge counts either way; an evidence line
        # cites a callee, not a mnemonic.
        calls.setdefault(call["function"], set()).add(call["to"] & ~1)
    for word in refs["words"]:
        for reader in word["readers"]:
            pool.setdefault(reader, set()).add(word["value"])
    return starts, calls, pool


def checked(rows):
    """Every row, with the claims it makes about the image still true.

    A name is worth no more than the reading behind it, so the callees and the
    globals the evidence cites are re-measured against the analysis this run
    reads. A body that no longer calls what the reading says it calls is a
    different body, and naming it would be the stale claim winning.
    """
    starts, calls, pool = load_analysis()
    out, complaints = [], []
    for row in rows:
        at = row["address"]
        fn = starts.get(at)
        if fn is None and row["kind"] != "label":
            complaints.append("0x%x is not a function start" % at)
            continue
        # An orphan label is code the partition gave no function of its own,
        # so the analysis attributes none of its calls to it and there is
        # nothing here to re-measure; the evidence line carries the reading.
        made = calls.get(at, set()) if fn else set(row.get("calls", ()))
        missing = [c for c in row.get("calls", ()) if c not in made]
        if missing:
            complaints.append("%s at 0x%x no longer calls %s"
                              % (row["name"], at,
                                 ", ".join("0x%x" % c for c in missing)))
            continue
        read = set()
        for site in range(at, fn["end"] if fn else at):
            read |= pool.get(site, set())
        absent = [v for v in row.get("reads", ()) if v not in read]
        if absent:
            complaints.append("%s at 0x%x no longer loads %s"
                              % (row["name"], at,
                                 ", ".join("0x%x" % v for v in absent)))
            continue
        out.append(dict(address=at, name=row["name"], kind=row["kind"],
                        **{"class": CLASS, "module": row["module"],
                           "evidence": row["evidence"]}))
    return out, complaints


def main():
    rows, complaints = checked(ROWS)
    for line in complaints:
        print("abi/stores.py: %s" % line, file=sys.stderr)
    if complaints:
        sys.exit("abi/stores.py: %d reading(s) no longer hold" % len(complaints))
    try:
        added = symmap.load().rewrite(rows, {CLASS},
                                      verified=set(r["address"] for r in rows))
    except symmap.Refusal as err:
        sys.exit("abi/stores.py: abi/symbols.yaml: %s" % err)
    print("%d storage-layer names checked against the analysis;"
          " %d entries added" % (len(rows), added))


if __name__ == "__main__":
    main()
