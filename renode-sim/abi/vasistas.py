#!/usr/bin/env python3
"""The vasistas payload layouts: the fake2 encoder, the walk's reply builders.

    python3 abi/vasistas.py          # writes the vasistas class of abi/symbols.yaml

A vasistas record's payload is bit-packed by type and nothing in the record
says how, so three tables have to agree before a field has a name. The shell's
`vasistas fake2` jumps through the table at 0x65eb8 into one packer per type,
`vasistas_record_size`'s tbb at 0x66534 gives the byte count, and the WPP walk
dispatches the same six-bit type through the tbh at 0x33556 into one reply
builder per type, whose unpacking of the record fills a wire object whose
fields abi/protocol.py already named. Where the packer's bit range and the
builder's bit range coincide, the wire object names the flash field; the ones
only one side touches are in abi/include/withings/stores.h as unknown_<bit>.

So a row here is a table row, and the check is that the table still resolves to
the address named: the three tables are re-read out of the image on every run,
their extents against the jump tables the export records, and a case that moved
is a refusal rather than a name that drifted off its evidence.
"""

import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import symbols as symmap  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
GHIDRA = os.path.join(HERE, "out", "ghidra")
CLASS = "vasistas"
APP_BASE = 0x27000

# The three dispatch tables, as the image holds them: the site the branch is
# at, where the entries start, and how a row becomes an address.
FAKE2 = dict(site=0x65EB2, start=0x65EB8, stride=4, entries=0x26,
             mnemonic="ldr.w")
SIZES = dict(site=0x66530, start=0x66534, stride=1, entries=0x3F,
             mnemonic="tbb", base=0x66534)

# The size table's arms, by the address the tbb sends a type to: the seven
# constants, the two that add to the header's own size field, and the two
# refusals. Ghidra gives 0x2ecf8..0x9460c one function and does not record this
# tbb among the jump tables, so the table is checked by what it resolves to.
SIZE_ARMS = {
    0x66574: 8, 0x66578: 12, 0x6657C: 32, 0x66594: 24, 0x66598: 20,
    0x665A6: 16, 0x665AA: 28,
    0x66580: "header size + 16", 0x6658A: "header size + 8",
    0x6659C: None, 0x665A2: None,
}

# Every storable type and the size the table gives it, which is the size the
# struct in stores.h is asserted against.
SIZE_OF_TYPE = {
    0: 8, 1: 28, 2: 28, 8: 28, 9: 16, 10: 16, 12: 16, 13: 12, 14: 16,
    15: "header size + 16", 16: 32, 18: 12, 19: "header size + 8", 20: 12,
    21: 24, 22: 12, 23: 16, 24: 12, 26: 28, 27: 20, 37: 12, 62: 8,
}
OBJECTS = dict(site=0x33552, start=0x33556, stride=2, entries=0x26,
               mnemonic="tbh", base=0x33556)

# address, name, kind, the table row that is the evidence, and the reading.
# `case` is (table, type id): the table must still send that type here.
ROWS = [
    # --- the shell's fake2 encoder, one packer per type --------------------
    dict(address=0x65F56, name="shell_fake2_store", kind="label",
         evidence="every packer falls through to it: vasistas_bank_for_record"
                  " for the record just built and then vasistas_store, with"
                  " 0xff meaning the type has no bank"),
    dict(address=0x65F6E, name="shell_fake2_pack_activity", kind="label",
         case=(FAKE2, 1),
         evidence="the 28-byte activity packing: steps at bits 55..63, level"
                  " at 64..68, distance at 75..90, descent at 96..109, reco_v2"
                  " at 110..122, calories at 128..140, met at 141..152 and the"
                  " merged bit at 174; types 2, 8, 26 and 37 share it and 37"
                  " stops after bit 74 (the cmp against 0x25 at 0x65fb8)"),
    dict(address=0x6604C, name="shell_fake2_pack_swim", kind="label",
         case=(FAKE2, 9),
         evidence="a whole word of duration at +8 then the 16-bit mvt at the"
                  " unaligned +0xd and laps at +0xf, which is the packing the"
                  " swim builder at 0x336b4 reads back"),
    dict(address=0x6607A, name="shell_fake2_pack_bkp", kind="label",
         case=(FAKE2, 10),
         evidence="the type vasistas_write_bkp writes: an 18-bit field at 64,"
                  " a 4-bit one at 82, 10-bit ones at 86, 96 and 106 (that one"
                  " packed in two pieces at 0x660e6 and 0x660fa) and two bits"
                  " at 116 and 117; no reply builder reads it"),
    dict(address=0x6613A, name="shell_fake2_pack_heartrate", kind="label",
         case=(FAKE2, 12),
         evidence="heartrate at bits 55..62, quality at +8, temperature at"
                  " +0xc, duration at +0xe and four flag bits in +0xf, which"
                  " is what the builder at 0x3371e sends as VasistasHeartrate"
                  " and VasistasFlags; type 14 shares the case and the"
                  " builder"),
    dict(address=0x6620C, name="shell_fake2_pack_activity_event", kind="label",
         case=(FAKE2, 13),
         evidence="an 8-bit event at bits 55..62 and a signed 16-bit"
                  " subcategory at +8; the builder at 0x337b0 matches the"
                  " event shifted left by 7 against 0x80..0x280"),
    dict(address=0x66236, name="shell_fake2_pack_unknown_16", kind="label",
         case=(FAKE2, 16),
         evidence="one four-bit field at bits 55..58 in a 32-byte record; the"
                  " reply builder's table has no row for type 16, so nothing"
                  " names it"),
    dict(address=0x66250, name="shell_fake2_pack_spo2", kind="label",
         case=(FAKE2, 18),
         evidence="spo2 at bits 64..73, error at 55..62 and quality at 74..81,"
                  " the three fields the builder at 0x338ee sends as"
                  " VasistasSpo2"),
    dict(address=0x66296, name="shell_fake2_pack_unknown_19", kind="label",
         case=(FAKE2, 19),
         evidence="the only case that takes no argument: it writes 34 into the"
                  " header's own size field (the bic/orr pair at 0x66296) and"
                  " copies 34 constant bytes to +8, which is why type 19's"
                  " size is the header's plus eight"),
    dict(address=0x662C8, name="shell_fake2_pack_ahi", kind="label",
         case=(FAKE2, 20),
         evidence="two halfwords at +8 and +0xa, the word the builder at"
                  " 0x33936 hands to VasistasAhi as ahi and bd_proba"),

    # --- the reply builders the WPP walk dispatches into --------------------
    dict(address=0x33534, name="vasistas_send_record", kind="function",
         evidence="masks the record's six-bit type and dispatches it through"
                  " the tbh at 0x33556; the default arm logs \"Vas type not"
                  " recognize\" at 0x33a46"),
    dict(address=0x33494, name="vasistas_send_head", kind="function",
         evidence="WamVasistasHead of a timestamp already made relative to the"
                  " session base at 0x3354a, or of zero when the second"
                  " argument equals it"),
    dict(address=0x33368, name="vasistas_send_duration", kind="function",
         evidence="WamVasistasDuration of its halfword argument; every builder"
                  " calls it straight after vasistas_send_head"),
    dict(address=0x33388, name="vasistas_send_awake", kind="function",
         evidence="WamVasistasAwake: steps from bits 55..63, distance from"
                  " 75..90, ascent from 160..173 and descent from 96..109,"
                  " that last one only when the byte at +5 of 0x20006448 is"
                  " zero"),
    dict(address=0x333D4, name="vasistas_send_met_cal", kind="function",
         evidence="calories from bits 128..140 and met from 141..152, sent as"
                  " WamVasistasMetCal or WamVasistasMetCalEarned depending on"
                  " the user's own two checks at 0x333da and 0x33404"),
    dict(address=0x33458, name="vasistas_send_acti_reco", kind="function",
         evidence="VasistasActiRecoV1V2 from bits 96..109 and 110..122, sent"
                  " only when the byte at +5 of 0x20006448 is not zero, which"
                  " is the same byte that decides whether 96..109 is descent"),
    dict(address=0x335A2, name="vasistas_send_activity_none", kind="label",
         case=(OBJECTS, 0),
         evidence="the head and the duration and nothing else, which is the"
                  " whole of an eight-byte type 0 record"),
    dict(address=0x335D4, name="vasistas_send_run", kind="label",
         case=(OBJECTS, 2),
         evidence="the activity set with WamVasistasRun for the level in bits"
                  " 64..68; type 1 differs only in sending WamVasistasWalk"),
    dict(address=0x33634, name="vasistas_send_sleep_short", kind="label",
         case=(OBJECTS, 37),
         evidence="head, duration and WamVasistasSleep of bits 64..68, the"
                  " three the 12-byte type 37 record can carry"),
    dict(address=0x3366A, name="vasistas_send_sleep", kind="label",
         case=(OBJECTS, 8),
         evidence="the activity set with WamVasistasSleep and no acti-reco"),
    dict(address=0x336B4, name="vasistas_send_swim", kind="label",
         case=(OBJECTS, 9),
         evidence="takes the head and the duration from the word at +8, then"
                  " Version and VasistasSwimType from the two nibbles of"
                  " +0xc and VasistasSwimV1 from +0xd and +0xf"),
    dict(address=0x3371E, name="vasistas_send_heartrate", kind="label",
         case=(OBJECTS, 12),
         evidence="VasistasHeartrate from bits 55..62, +8 and +0xc with the"
                  " duration at +0xe, then VasistasFlags from the low four"
                  " bits of +0xf against a constant supported mask of 0xf,"
                  " the fourth of them inverted (the `pl` arm at 0x33798)"),
    dict(address=0x337B0, name="vasistas_send_activity_event", kind="label",
         case=(OBJECTS, 13),
         evidence="ActivitySubcategory, ActivityLap or ActivityPause by the"
                  " event code in bits 55..62, keeping the previous event's"
                  " timestamp in 0x20017c50 and 0x20017c4c so the next one"
                  " carries a duration"),
    dict(address=0x338EE, name="vasistas_send_spo2", kind="label",
         case=(OBJECTS, 18),
         evidence="the duration from bits 82..89, then VasistasSpo2 from"
                  " 64..73, 74..81 and 55..62"),
    dict(address=0x33936, name="vasistas_send_ahi", kind="label",
         case=(OBJECTS, 20),
         evidence="VasistasAhi straight out of the word at +8"),
    dict(address=0x3395C, name="vasistas_send_cbt", kind="label",
         case=(OBJECTS, 22),
         evidence="VasistasCbt: algo from bits 55..57, attrib from 58..61 and"
                  " a signed 20-bit temperature from 64..83 (the sbfx at"
                  " 0x3399e); an attrib of 5 sends the duration as zero and"
                  " anything past 90 seconds is clamped to 60"),
    dict(address=0x339AC, name="vasistas_send_hrv", kind="label",
         case=(OBJECTS, 23),
         evidence="the duration from +0xd, then VasistasHrv from +7, +8, +0xa"
                  " and +0xc"),
    dict(address=0x339E6, name="vasistas_send_rr", kind="label",
         case=(OBJECTS, 24),
         evidence="the duration from +0xa and VasistasRr from +8"),
    dict(address=0x33A0A, name="vasistas_send_dbt", kind="label",
         case=(OBJECTS, 27),
         evidence="the duration from the halfword at +8 and VasistasDbt's nine"
                  " bytes from +0xa to +0x12"),

    # --- the tables themselves ---------------------------------------------
    dict(address=0x65EB8, name="shell_fake2_case_table", kind="data",
         table=FAKE2,
         evidence="38 absolute targets, one per record type; a type with no"
                  " packing points at the usage line at 0x65d56"),
    dict(address=0x66534, name="vasistas_record_size_table", kind="data",
         sizes=True,
         evidence="63 byte offsets off 0x66534: the fixed sizes 8, 12, 16, 20,"
                  " 24, 28 and 32, the two variable arms that add 8 and 16 to"
                  " the header's own size field, and zero for a type that is"
                  " not storable"),
    dict(address=0x33556, name="vasistas_wpp_object_table", kind="data",
         table=OBJECTS,
         evidence="38 halfword offsets off 0x33556, one reply builder per"
                  " record type; this is the map from a flash type to the wire"
                  " objects it is sent as"),
]


def image():
    with open(os.path.join(SIM, "appl.bin"), "rb") as fh:
        return fh.read()


def resolve(blob, table, type_id):
    """The address a table row branches to, the way the branch does it."""
    at = table["start"] + type_id * table["stride"] - APP_BASE
    if table["stride"] == 4:
        return struct.unpack_from("<I", blob, at)[0] & ~1
    if table["stride"] == 2:
        return table["base"] + 2 * struct.unpack_from("<H", blob, at)[0]
    return table["base"] + 2 * blob[at]


def checked(rows):
    """Every row, against the tables and the analysis this run reads.

    A packer or a builder is worth exactly the table row that reaches it, so
    the table is re-read out of the image and a case that no longer lands on
    the address named is dropped with a complaint. The extents come from the
    export's own record of the jump tables, so a re-analysis that moves a
    table is caught as well.
    """
    blob = image()
    with open(os.path.join(GHIDRA, "items.json")) as fh:
        items = json.load(fh)
    with open(os.path.join(GHIDRA, "references.json")) as fh:
        refs = json.load(fh)
    starts = set(f["start"] for f in items["functions"])
    tables = dict((t["site"], t) for t in refs["jump_tables"])

    out, complaints = [], []
    for row in rows:
        at = row["address"]
        if row["kind"] == "function" and at not in starts:
            complaints.append("0x%x is not a function start" % at)
            continue
        table = row.get("table")
        if table is not None:
            found = tables.get(table["site"])
            if found is None:
                complaints.append("%s: the export records no jump table at"
                                  " 0x%x" % (row["name"], table["site"]))
                continue
            if (found["start"], found["stride"]) != (table["start"],
                                                     table["stride"]):
                complaints.append("%s: the export puts the table at 0x%x"
                                  " stride %d, not 0x%x stride %d"
                                  % (row["name"], found["start"],
                                     found["stride"], table["start"],
                                     table["stride"]))
                continue
        if row.get("sizes"):
            given, broken = {}, False
            for type_id in range(SIZES["entries"]):
                arm = resolve(blob, SIZES, type_id)
                if arm not in SIZE_ARMS:
                    complaints.append("%s: type %d reaches 0x%x, which is not"
                                      " one of the arms"
                                      % (row["name"], type_id, arm))
                    broken = True
                    break
                if SIZE_ARMS[arm] is not None:
                    given[type_id] = SIZE_ARMS[arm]
            if broken:
                continue
            if given != SIZE_OF_TYPE:
                complaints.append("%s: the table now gives %s"
                                  % (row["name"], given))
                continue
            out.append(dict(address=at, name=row["name"], kind=row["kind"],
                            **{"class": CLASS, "module": "VASISTAS",
                               "evidence": row["evidence"]}))
            continue
        case = row.get("case")
        if case is not None:
            where = resolve(blob, case[0], case[1])
            if where != at:
                complaints.append("%s: type %d now reaches 0x%x, not 0x%x"
                                  % (row["name"], case[1], where, at))
                continue
        out.append(dict(address=at, name=row["name"], kind=row["kind"],
                        **{"class": CLASS, "module": "VASISTAS",
                           "evidence": row["evidence"]}))
    return out, complaints


def main():
    rows, complaints = checked(ROWS)
    for line in complaints:
        print("abi/vasistas.py: %s" % line, file=sys.stderr)
    if complaints:
        sys.exit("abi/vasistas.py: %d reading(s) no longer hold"
                 % len(complaints))
    try:
        added = symmap.load().rewrite(rows, {CLASS})
    except symmap.Refusal as err:
        sys.exit("abi/vasistas.py: abi/symbols.yaml: %s" % err)
    print("%d vasistas names checked against the three dispatch tables;"
          " %d entries added" % (len(rows), added))


if __name__ == "__main__":
    main()
