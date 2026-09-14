#!/usr/bin/env python3
"""Boot progress bar from a Renode PC execution trace.

Usage:  python3 coverage.py <trace.bin[.gz]> [symbols.txt]

Renode trace format: b"ReTrace" + 3 header bytes, then 5-byte entries
(little-endian u32 PC + 1 flag byte). Reads the whole run, and reports the
known functions (from symbols.txt) in first-execution order plus the loop the
run ends stuck in. Coverage, not logs, is the progress signal.
"""
import sys, gzip, struct, bisect, pathlib

HERE = pathlib.Path(__file__).parent


def load_symbols(path):
    syms = []
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 2)
        if len(parts) < 2 or not parts[0].startswith("0x"):
            continue
        addr = int(parts[0], 16)
        name = parts[1]
        note = parts[2] if len(parts) > 2 else ""
        syms.append((addr, name, note))
    syms.sort()
    return syms


def nearest(syms, addrs, pc):
    """Nearest symbol at or below pc (the function pc is inside), within 0x2000."""
    i = bisect.bisect_right(addrs, pc) - 1
    if i < 0:
        return None
    a, name, note = syms[i]
    if pc - a > 0x2000:
        return None
    return (a, name, note, pc - a)


def open_trace(path):
    with open(path, "rb") as f:
        head = f.read(2)
    raw = gzip.open(path, "rb") if head == b"\x1f\x8b" else open(path, "rb")
    magic = raw.read(10)
    if magic[:7] != b"ReTrace":
        sys.exit(f"not a Renode trace (magic={magic[:7]!r})")
    return raw


def main():
    trace = sys.argv[1]
    symfile = sys.argv[2] if len(sys.argv) > 2 else str(HERE / "symbols.txt")
    syms = load_symbols(symfile)
    addrs = [s[0] for s in syms]
    symset = set(addrs)

    raw = open_trace(trace)
    entry = struct.Struct("<IB")
    total = 0
    first_hit = []          # (order_index, pc) for known symbol entries, first time only
    seen = set()
    TAIL = 400
    tail = [0] * TAIL       # ring buffer of last PCs

    while True:
        try:
            chunk = raw.read(5 * 200000)
        except EOFError:           # truncated trace (run killed mid-write) — use what we have
            break
        if not chunk:
            break
        if len(chunk) % 5:
            chunk = chunk[: len(chunk) // 5 * 5]
        for pc, _flag in entry.iter_unpack(chunk):
            if pc in symset and pc not in seen:
                seen.add(pc)
                first_hit.append((total, pc))
            tail[total % TAIL] = pc
            total += 1

    symidx = {a: (a, n, note) for a, n, note in syms}
    print(f"# trace: {trace}   entries: {total:,}")
    print(f"# boot path — known functions in first-execution order:")
    for order, pc in first_hit:
        _, name, note = symidx[pc]
        pct = 100.0 * order / total if total else 0
        print(f"  {pct:5.1f}%  {pc:#010x}  {name:<28} {note}")

    # final stall: distinct PCs in the tail, mapped to functions
    span = tail if total >= TAIL else tail[:total]
    order_seen = []
    d = set()
    for pc in span:
        if pc not in d:
            d.add(pc)
            order_seen.append(pc)
    print(f"\n# final stall — loop over the last {len(span)} executed PCs "
          f"({len(order_seen)} distinct):")
    funcs = []
    for pc in sorted(order_seen):
        m = nearest(syms, addrs, pc)
        tag = f"{m[1]}+{m[3]:#x}" if m else "?"
        funcs.append((pc, tag))
    shown = funcs[:24]
    for pc, tag in shown:
        print(f"    {pc:#010x}  {tag}")
    if len(funcs) > len(shown):
        print(f"    ... (+{len(funcs)-len(shown)} more)")


if __name__ == "__main__":
    main()
