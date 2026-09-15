#!/usr/bin/env python3
"""Emit Renode hooks that print the first entry into every function called from a code range.

    python3 calltrace.py 0x2e700 0x2e900 > out/trace-init.resc
    renode ... -e "include @display-run.resc-like setup; include @out/trace-init.resc; emulation RunFor \"3\""

Reads out/appl.dis (run mkdis.sh first). Each hooked callee prints its address, the
caller's return address and the virtual time in microseconds; a second flag hooks the
call sites themselves instead. This is how the boot's stall points were found: hook the
callees of one init function, look at the last line printed.
"""
import argparse, pathlib, re, sys

HERE = pathlib.Path(__file__).parent

HOOK = '''cpu AddHook 0x{addr:x} """
print 'CALL {addr:x} lr=%x r0=%x r1=%x vt=%.0f' % (cpu.GetRegisterUnsafe(14).RawValue, cpu.GetRegisterUnsafe(0).RawValue, cpu.GetRegisterUnsafe(1).RawValue, cpu.GetMachine().ElapsedVirtualTime.TimeElapsed.TotalMicroseconds)
"""
'''

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lo", type=lambda x: int(x, 0))
    ap.add_argument("hi", type=lambda x: int(x, 0))
    ap.add_argument("--dis", default=str(HERE / "out" / "appl.dis"))
    args = ap.parse_args()
    callees = set()
    for line in open(args.dis):
        m = re.match(r"\s+([0-9a-f]+):\s+\S+ \S+\s+bl\s+0x([0-9a-f]+)", line)
        if m and args.lo <= int(m.group(1), 16) < args.hi:
            callees.add(int(m.group(2), 16))
    if not callees:
        sys.exit("no bl in range; is out/appl.dis built?")
    for addr in sorted(callees):
        sys.stdout.write(HOOK.format(addr=addr))
    sys.stderr.write("%d callees hooked\n" % len(callees))

if __name__ == "__main__":
    main()
