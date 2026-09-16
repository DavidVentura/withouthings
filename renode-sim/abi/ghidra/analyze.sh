#!/bin/bash
# Partition the HWA10 app image with Ghidra, headless and from scratch.
#
#   abi/ghidra/analyze.sh            # ~/ref-build/ghidra-project is rebuilt every run
#
# The app slice of flash.bin (0x27000..0xf117c) is imported raw as
# ARM:LE:32:Cortex at 0x27000, seeded with every name the repo knows
# (abi/ghidra/seed.py -> abi/ghidra/seed_symbols.py) and analysed; then
# abi/ghidra/export_partition.py writes abi/out/ghidra/{items,references,
# symbols}.* . abi/ghidra/crosscheck.py checks the result against appl.dis and
# the hand maps.
#
# Scripts run as CPython 3 under PyGhidra, which is why Ghidra is launched
# through pyghidra's ghidra_launch rather than support/analyzeHeadless: the
# stock launcher starts the JVM alone and cannot host python scripts.
set -eu
set -o pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ABI=$(dirname "$HERE")
SIM=$(dirname "$ABI")
ROOT=${ROOT:-$HOME/ref-build}
OUT=$ABI/out/ghidra
PROJ=$ROOT/ghidra-project

GHIDRA=$("$HERE/fetch.sh")
PY=$ROOT/ghidra/venv/bin/python
[ -x "$PY" ] || { echo "no pyghidra venv: $PY" >&2; exit 1; }

mkdir -p "$OUT"

# The imported bytes must be the image's own app slice, not a stale appl.bin.
python3 - "$SIM/flash.bin" "$SIM/appl.bin" <<'PY'
import sys
flash, appl = (open(p, "rb").read() for p in sys.argv[1:3])
if flash[0x27000:0x27000 + len(appl)] != appl or len(appl) != 0xf117c - 0x27000:
    sys.exit("appl.bin is not flash.bin[0x27000:0xf117c]")
PY

python3 "$HERE/seed.py" -o "$OUT/seed.json"

rm -rf "$PROJ"
mkdir -p "$PROJ"
start=$(date +%s)
"$PY" -m pyghidra.ghidra_launch --install-dir "$GHIDRA" \
    -Dghidra.repositories.dir="$PROJ" \
    ghidra.app.util.headless.AnalyzeHeadless \
    "$PROJ" hwa10 \
    -import "$SIM/appl.bin" \
    -processor ARM:LE:32:Cortex \
    -loader BinaryLoader -loader-baseAddr 0x27000 \
    -scriptPath "$HERE" \
    -preScript seed_symbols.py "$OUT/seed.json" \
    -postScript export_partition.py "$OUT" \
    -log "$OUT/analysis.log" -scriptlog "$OUT/script.log" 2>&1 | tee "$OUT/headless.log"

# analyzeHeadless logs a failing script and carries on; a half-seeded or
# half-exported run must not look like a success.
if grep -q "^ERROR" "$OUT/headless.log"; then
    grep -A5 "^ERROR" "$OUT/headless.log" >&2
    exit 1
fi
echo "analysis wall time: $(( $(date +%s) - start ))s"

python3 "$HERE/crosscheck.py" --out "$OUT"
