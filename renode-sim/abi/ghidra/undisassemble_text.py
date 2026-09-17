# Ghidra post-script: take back the disassembly of the runs that are text.
#
# Run by abi/ghidra/analyze.sh between close_partition.py and classify_gaps.py.
#
# The linear disassembler decodes anything, and 3.7 KB of this image's string
# tables -- the French, German and Spanish translations, the WPP command names
# and mbedTLS's OID descriptions -- decode as Thumb and end up inside function
# bodies. That is not a cosmetic error: the classification's rule that an even
# address inside code is a number, which holds because this image is Thumb
# throughout, then turns every pointer into one of those strings into a
# constant, the string moves with the code section it was swallowed by and the
# word is left naming nothing. The tail-shared "en" of "Nicht geladen" at
# 0xc8496 is one: the watch printed "move_hands feature abled" under a moved
# layout until this ran.
#
# A region is taken back only when it is unmistakably text, because Thumb reads
# as printable far too easily: `mov r0,r4` is 0x46 0x20, which is "F ", and a
# stretch of register moves looks like a sentence. So a text byte is ASCII or a
# well-formed two-byte UTF-8 sequence and nothing else, and the region has to
# hold at least two NUL-terminated words of four characters that are more than
# half letters. A branch into the region from outside it still refuses the
# clear; a branch from inside it is the bad disassembly talking about itself.
# @category HWA10

from ghidra.program.model.address import AddressSet

APP_BASE = 0x27000
APP_END = 0xF117C

MIN_WORDS = 2
MIN_WORD = 4
MIN_CODE = 16

listing = currentProgram.getListing()
fm = currentProgram.getFunctionManager()
rm = currentProgram.getReferenceManager()
space = currentProgram.getAddressFactory().getDefaultAddressSpace()
app_set = AddressSet(space.getAddress(APP_BASE), space.getAddress(APP_END - 1))

image = bytearray(b & 0xFF for b in
                  getBytes(space.getAddress(APP_BASE), APP_END - APP_BASE))


def textual(at):
    """ASCII, or the two bytes of a Latin-1 character in UTF-8, and nothing else."""
    c = image[at - APP_BASE]
    if c == 0 or 0x20 <= c < 0x7F or c in (9, 10, 13):
        return True
    nxt = image[at - APP_BASE + 1] if at + 1 < APP_END else 0
    if c in (0xC2, 0xC3) and 0x80 <= nxt < 0xC0:
        return True
    return 0x80 <= c < 0xC0 and at > APP_BASE and image[at - APP_BASE - 1] in (0xC2, 0xC3)


def words_in(lo, hi):
    """How many NUL-terminated words read as words rather than as instructions."""
    found = 0
    for word in bytes(image[lo - APP_BASE:hi - APP_BASE]).split(b"\0"):
        letters = sum(1 for c in word if 0x41 <= c < 0x5B or 0x61 <= c < 0x7B)
        if len(word) >= MIN_WORD and letters >= max(3, len(word) // 2):
            found += 1
    return found


regions, at = [], APP_BASE
while at < APP_END:
    if not textual(at):
        at += 1
        continue
    end = at
    while end < APP_END and textual(end):
        end += 1
    if words_in(at, end) >= MIN_WORDS:
        regions.append((at, end))
    at = end


def is_code(addr):
    return listing.getInstructionContaining(space.getAddress(addr)) is not None


def soft_text(at):
    """Text for the purpose of judging a whole function body.

    A body that reaches a little past the run the strict test found is the same
    table with an escape sequence or a stray byte in it, not code.
    """
    c = image[at - APP_BASE]
    return textual(at) or c == 0x1B or c < 0x20


cleared, refused = 0, []
for lo, hi in regions:
    covered = [a for a in range(lo, hi) if is_code(a)]
    if len(covered) < MIN_CODE:
        continue
    start, end = min(covered), max(covered) + 1
    # An instruction the disassembly started before the region can reach into
    # it, so the extent to clear is whole code units.
    unit = listing.getCodeUnitContaining(space.getAddress(start))
    if unit is not None:
        start = unit.getMinAddress().getOffset()
    # A function that starts before the region still owns the bytes inside it,
    # and a body claiming bytes the listing no longer calls instructions makes
    # the export's cover overlap itself. So the unit of the decision is the
    # whole body of every function the region touches, and it is only taken
    # back when that whole body reads as text too.
    area = AddressSet(space.getAddress(start), space.getAddress(end - 1))
    touched = list(fm.getFunctionsOverlapping(area))
    for fn in touched:
        body = fn.getBody()
        start = min(start, body.getMinAddress().getOffset())
        end = max(end, body.getMaxAddress().getOffset() + 1)
    prose = sum(1 for a in range(start, end) if soft_text(a))
    if prose * 10 < (end - start) * 9:
        refused.append((start, end, "%d of %d bytes are text" % (prose, end - start)))
        continue
    aimed = [a for a in range(start, end)
             if any(r.getReferenceType().isFlow()
                    and not start <= r.getFromAddress().getOffset() < end
                    for r in rm.getReferencesTo(space.getAddress(a)))]
    if aimed:
        refused.append((start, end, "0x%x is a branch target" % aimed[0]))
        continue
    for fn in touched:
        fm.removeFunction(fn.getEntryPoint())
    listing.clearCodeUnits(space.getAddress(start), space.getAddress(end - 1), True)
    cleared += end - start

for start, end, why in refused:
    println("undisassemble_text: 0x%x..0x%x reads as text but %s, so it is left"
            " as code" % (start, end, why))
println("undisassemble_text: %d text regions, %d bytes of disassembly cleared,"
        " %d refused" % (len(regions), cleared, len(refused)))
