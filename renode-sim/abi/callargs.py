#!/usr/bin/env python3
"""What the calling sequence puts in the argument registers of a call.

The back-walk in abi/autonames.py answers one question -- which literal-pool
string each register was last loaded with -- and answers it by scanning
backwards until it has seen a write to every register it cares about. That is
enough for a logger whose format and varargs are pool loads in the same basic
block, and it is blind to everything else: a value moved between registers
before the call, an argument computed from a pool word by an add, an argument
stored into the stack for a call that takes more than four, and the common case
where the value is not a literal at all but a field of the caller's own struct.

This is the same walk written forwards over the block instead, with an abstract
value per register rather than a string per register. The block is the run of
instructions ending at the call and beginning after the last control transfer,
which is the largest window in which a register's last write is the value the
call sees whatever the control flow around it does. Every argument comes out of
it as one of five things, and a shape (`Field`, `Table`, `Unknown`) is a
finding rather than a failure: it says where the value comes from without
claiming to know it, which is what a caller that wants to report rather than
guess needs.
"""

import re

# A control transfer ends the block: past it a register's last write in the
# straight line is not the write the call sees. `pop` is here because it is the
# tail of a return, so the instructions before it belong to another path.
BOUNDARY = ("b", "b.w", "bx", "blx", "bl", "bl.w", "pop", "pop.w", "cbz",
            "cbnz", "tbb", "tbh", "ldm", "ldm.w", "ldmia")
MAX_BLOCK = 48

DEST = re.compile(r"^(r\d+|sp|lr|pc)\s*,")
POOL = re.compile(r"@ 0x([0-9a-f]+)")
IMM = re.compile(r"^(r\d+),\s*#(-?(?:0x)?[0-9a-fA-F]+)$")
MOVE = re.compile(r"^(r\d+),\s*(r\d+)$")
ADD3 = re.compile(r"^(r\d+),\s*(r\d+),\s*#(-?(?:0x)?[0-9a-fA-F]+)$")
ADD2 = re.compile(r"^(r\d+),\s*#(-?(?:0x)?[0-9a-fA-F]+)$")
FIELD = re.compile(r"^(r\d+),\s*\[(r\d+|sp)(?:,\s*#(-?(?:0x)?[0-9a-fA-F]+))?\]!?$")
INDEXED = re.compile(r"^(r\d+),\s*\[(r\d+),\s*(r\d+)")
STACK = re.compile(r"^(r\d+),\s*\[sp(?:,\s*#(0x[0-9a-fA-F]+|\d+))?\]$")


class Value(object):
    """What a register holds at a call, as far as the block establishes it."""

    kind = "unknown"

    def __repr__(self):
        return "<%s %s>" % (self.kind, self.text())

    def text(self):
        raise NotImplementedError


class Imm(Value):
    """A literal the block materialised into the register."""

    kind = "imm"

    def __init__(self, value):
        self.value = value & 0xFFFFFFFF

    def text(self):
        return "0x%x" % self.value


class Pool(Value):
    """A word the block loaded out of the literal pool, and where from."""

    kind = "pool"

    def __init__(self, value, at):
        self.value = value & 0xFFFFFFFF
        self.at = at

    def text(self):
        return "0x%x (pool 0x%x)" % (self.value, self.at)


class Field(Value):
    """A load through a pointer: the value is in RAM and the block is not it."""

    kind = "field"

    def __init__(self, base, offset, width):
        self.base = base
        self.offset = offset
        self.width = width

    def text(self):
        return "%s [%s,#0x%x]" % (self.width, self.base, self.offset)


class Table(Value):
    """A load at a computed index: one row of something, and not knowably which."""

    kind = "table"

    def __init__(self, base, index):
        self.base = base
        self.index = index

    def text(self):
        return "[%s + %s]" % (self.base, self.index)


class Param(Value):
    """The nth argument the enclosing function was itself called with.

    A register the block never wrote, in a block that reaches the function's
    own entry, still holds what the caller put there. The value is one hop
    further back rather than unknown, which is what lets a reader ask the
    callers the same question.
    """

    kind = "param"

    def __init__(self, index):
        self.index = index

    def text(self):
        return "arg%d" % self.index


class Unknown(Value):
    """Written by something this does not model, or never written in the block."""

    kind = "unknown"

    def __init__(self, why):
        self.why = why

    def text(self):
        return self.why


def _int(text):
    return int(text, 16) if text.lower().startswith(("0x", "-0x")) else int(text, 10)


def block_start(insns, call):
    """The index the straight line ending at `call` begins at."""
    i = call
    while i > 0 and call - i < MAX_BLOCK:
        if insns[i - 1][1] in BOUNDARY:
            break
        i -= 1
    return i


class Frame(object):
    """The register file and the outgoing stack slots at one call."""

    def __init__(self, regs, stack):
        self.regs = regs
        self.stack = stack

    def reg(self, n):
        return self.regs.get("r%d" % n, Unknown("never written in the block"))

    def arg(self, n):
        """The nth AAPCS argument: r0..r3, then the outgoing stack slots."""
        if n < 4:
            return self.reg(n)
        return self.stack.get(4 * (n - 4), Unknown("stack slot never written"))


def resolve(img, insns, call, initial=None, start=None):
    """The frame a call is made with, from the block that sets it up.

    `initial` is what the registers hold on entry to the block, which the block
    itself cannot say: the callee-saved registers a function loads once and the
    argument registers it was called with. Everything the block writes lands on
    top of it in the usual way.

    `start` widens the window past the last control transfer, for a caller that
    has established that the instructions before it are on every path into the
    call. The default is the straight line, which needs no such argument.

    Forward abstract interpretation over the block: every write lands in the
    register file, so a move chain, an add on a pool word and a store into an
    outgoing stack slot all carry, and a write this does not model poisons its
    destination instead of leaving the register's older value standing.
    """
    regs, stack = dict(initial or {}), {}
    for i in range(block_start(insns, call) if start is None else start, call):
        _, mnem, ops = insns[i]
        if mnem.startswith("str"):
            slot = STACK.match(ops)
            if slot:
                stack[_int(slot.group(2)) if slot.group(2) else 0] = \
                    regs.get(slot.group(1), Unknown("stored before it was written"))
            continue
        if mnem.startswith("it"):
            # A predicated write is a write on one path only, so nothing in the
            # IT block may be taken as the value the call sees.
            for j in range(i + 1, min(i + 1 + len(mnem) - 1, call)):
                dest = DEST.match(insns[j][2])
                if dest:
                    regs[dest.group(1)] = Unknown("written under an IT block")
            continue
        dest = DEST.match(ops)
        if not dest or not dest.group(1).startswith("r"):
            continue
        regs[dest.group(1)] = _value(img, regs, mnem, ops)
    return Frame(regs, stack)


def _value(img, regs, mnem, ops):
    dest = DEST.match(ops).group(1)
    if mnem.startswith("ldr"):
        if "[pc" in ops:
            at = POOL.search(ops)
            if not at:
                return Unknown("pc-relative load the disassembly does not annotate")
            word = img.word(int(at.group(1), 16) + img.base)
            if word is None:
                return Unknown("pool word outside the image")
            return Pool(word, int(at.group(1), 16) + img.base)
        indexed = INDEXED.match(ops)
        if indexed:
            return Table(indexed.group(2), indexed.group(3))
        field = FIELD.match(ops)
        if field:
            width = {"ldr": "u32", "ldr.w": "u32", "ldrb": "u8", "ldrb.w": "u8",
                     "ldrh": "u16", "ldrh.w": "u16", "ldrsb": "i8",
                     "ldrsh": "i16"}.get(mnem, mnem)
            base = regs.get(field.group(2))
            # A field of something whose address is known is a field of a known
            # object, and naming the address is what makes that legible. An Imm
            # here is a pool word an `add` displaced, which is still an address.
            if isinstance(base, (Pool, Imm)):
                base = "0x%x" % base.value
            else:
                base = field.group(2)
            return Field(base, _int(field.group(3)) if field.group(3) else 0, width)
        return Unknown("%s %s" % (mnem, ops))
    if mnem in ("mov", "mov.w", "movs", "movw", "mvn", "mvns"):
        move = MOVE.match(ops)
        if move:
            held = regs.get(move.group(2), Unknown("never written in the block"))
            return held
        imm = IMM.match(ops)
        if imm:
            value = _int(imm.group(2))
            return Imm(~value if mnem.startswith("mvn") else value)
        return Unknown("%s %s" % (mnem, ops))
    if mnem in ("add", "add.w", "adds", "sub", "sub.w", "subs"):
        sign = -1 if mnem.startswith("sub") else 1
        three, two = ADD3.match(ops), ADD2.match(ops)
        if three:
            base, delta = regs.get(three.group(2)), sign * _int(three.group(3))
        elif two:
            base, delta = regs.get(two.group(1)), sign * _int(two.group(2))
        else:
            return Unknown("%s %s" % (mnem, ops))
        if isinstance(base, Imm):
            return Imm(base.value + delta)
        if isinstance(base, Pool):
            return Imm(base.value + delta)
        return Unknown("%s %s" % (mnem, ops))
    return Unknown("%s %s" % (mnem, ops))


def string_of(img, value, limit=200):
    """The image string a value points at, or None if it is not one."""
    if not isinstance(value, (Pool, Imm)):
        return None
    return img.string(value.value, limit=limit)
