#!/usr/bin/env python3
"""Emit Renode hooks that print the first entry into every function called from a code range.

    python3 calltrace.py 0x2e700 0x2e900 > out/trace-init.resc
    python3 calltrace.py --module ECG > out/trace-ecg.resc
    python3 calltrace.py --module ECG --count > out/trace-ecg-count.resc
    renode ... -e "include @display-run.resc-like setup; include @out/trace-init.resc; emulation RunFor \"3\""

Reads out/appl.dis (run mkdis.sh first). Each hooked callee prints its address, the
caller's return address and the virtual time in microseconds. This is how the boot's
stall points were found: hook the callees of one init function, look at the last line
printed.

`--module` hooks a whole module of abi/out/ghidra/modules.json instead of the callees of
a range, which is what a library of signal-processing functions needs: they are scattered
and they call each other, so the range that would cover them covers half the image.
`--count` makes each hook count instead of print, and `--report` prints the counts; the
ratio between two functions' counts over one run is what says which is per sample, which
is per block and which is per measurement, and a per-sample function called nine thousand
times cannot be printed.
"""
import argparse, json, pathlib, re, sys

HERE = pathlib.Path(__file__).parent

HOOK = '''cpu AddHook 0x{addr:x} """
print 'CALL {addr:x} lr=%x r0=%x r1=%x vt=%.0f' % (cpu.GetRegisterUnsafe(14).RawValue, cpu.GetRegisterUnsafe(0).RawValue, cpu.GetRegisterUnsafe(1).RawValue, cpu.GetMachine().ElapsedVirtualTime.TimeElapsed.TotalMicroseconds)
"""
'''

# Each hook gets its own Python scope, so the counter cannot live in the hook's
# globals: it goes on the `sys` module, which is the one object every scope in
# the process shares.
COUNT_HOOK = '''cpu AddHook 0x{addr:x} """
import sys
if not hasattr(sys, 'calltrace'): sys.calltrace = dict()
sys.calltrace[0x{addr:x}] = sys.calltrace.get(0x{addr:x}, 0) + 1
"""
'''

# Counting loses the one thing a scenario trace is for: which function ran
# first. The entry keeps a count, the sequence number of its first entry and
# the virtual time of it, so a rate and an order come out of the same run.
ORDER_HOOK = '''cpu AddHook 0x{addr:x} """
import sys
if not hasattr(sys, 'calltrace'): sys.calltrace = dict(); sys.calltrace_n = [0]
sys.calltrace_n[0] += 1
e = sys.calltrace.setdefault(0x{addr:x}, [0, 0, 0.0])
e[0] += 1
if e[1] == 0:
    e[1] = sys.calltrace_n[0]
    e[2] = cpu.GetMachine().ElapsedVirtualTime.TimeElapsed.TotalMicroseconds
"""
'''

REPORT = '''cpu AddHook 0x{addr:x} """
import sys
for k in sorted(getattr(sys, 'calltrace', dict())):
    print 'COUNT %x %d' % (k, sys.calltrace[k])
"""
'''


def module_functions(path, module):
    """Every function abi/autonames.py's log-tag partition puts in one module."""
    with open(path) as fh:
        partition = json.load(fh)
    return sorted(int(a, 16) for a, v in partition["functions"].items()
                  if v["module"] == module)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lo", nargs="?", type=lambda x: int(x, 0))
    ap.add_argument("hi", nargs="?", type=lambda x: int(x, 0))
    ap.add_argument("--dis", default=str(HERE / "out" / "appl.dis"))
    ap.add_argument("--module", help="hook every function of this modules.json module")
    ap.add_argument("--modules", default=str(HERE / "abi" / "out" / "ghidra" / "modules.json"))
    ap.add_argument("--also", action="append", default=[], type=lambda x: int(x, 0),
                    help="one more address to hook, for a function the partition"
                         " puts in another module")
    ap.add_argument("--count", action="store_true",
                    help="count the entries instead of printing them")
    ap.add_argument("--order", action="store_true",
                    help="count, and keep the first entry's position and time")
    ap.add_argument("--addresses",
                    help="a file of addresses, one per line, to hook instead of"
                         " a module or a range; a set a trace was asked for by"
                         " some other rule than the log-tag partition")
    ap.add_argument("--report", type=lambda x: int(x, 0),
                    help="the address whose entry dumps the counts; give the"
                         " function that ends the run")
    args = ap.parse_args()
    if args.addresses:
        hooked = set(int(line.split()[0], 16) for line in open(args.addresses)
                     if line.strip() and not line.startswith("#"))
    elif args.module:
        hooked = set(module_functions(args.modules, args.module))
    elif args.lo is not None and args.hi is not None:
        hooked = set()
        for line in open(args.dis):
            m = re.match(r"\s+([0-9a-f]+):\s+\S+ \S+\s+bl\s+0x([0-9a-f]+)", line)
            if m and args.lo <= int(m.group(1), 16) < args.hi:
                hooked.add(int(m.group(2), 16))
        if not hooked:
            sys.exit("no bl in range; is out/appl.dis built?")
    else:
        sys.exit("give a range or --module")
    hooked.update(args.also)
    template = ORDER_HOOK if args.order else COUNT_HOOK if args.count else HOOK
    for addr in sorted(hooked):
        if addr == args.report:
            continue
        sys.stdout.write(template.format(addr=addr))
    if args.report is not None:
        sys.stdout.write(REPORT.format(addr=args.report))
    sys.stderr.write("%d functions hooked\n" % len(hooked))

if __name__ == "__main__":
    main()
