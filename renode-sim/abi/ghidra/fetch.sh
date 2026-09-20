#!/bin/bash
# Fetch and unpack the Ghidra release used for the app partition, the way
# abi/refbuild.sh fetches toolchains: idempotent, into ~/ref-build, nothing in
# the repo.
#
#   abi/ghidra/fetch.sh          # prints the unpacked GHIDRA_DIR on stdout
#
# The SHA-256 is the one the release page states for the zip; a mismatch is
# fatal, never a warning.
set -eu

ROOT=${ROOT:-$HOME/ref-build}
VER=12.1.3
STAMP=20260817
ZIP=ghidra_${VER}_PUBLIC_${STAMP}.zip
URL=https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_${VER}_build/$ZIP
SHA=93a5d11a9ad510622acaaf908c556a7b9b764d338e78a7567f3689bf5081fd54
DIR=$ROOT/ghidra/ghidra_${VER}_PUBLIC

if [ ! -x "$DIR/support/analyzeHeadless" ]; then
    mkdir -p "$ROOT/dl" "$ROOT/ghidra"
    if [ ! -f "$ROOT/dl/$ZIP" ]; then
        curl -fL -o "$ROOT/dl/$ZIP.part" "$URL"
        mv "$ROOT/dl/$ZIP.part" "$ROOT/dl/$ZIP"
    fi
    echo "$SHA  $ROOT/dl/$ZIP" | sha256sum -c -
    unzip -q "$ROOT/dl/$ZIP" -d "$ROOT/ghidra"
fi
[ -x "$DIR/support/analyzeHeadless" ] || { echo "ghidra unpack incomplete: $DIR" >&2; exit 1; }

# PyGhidra, from the wheels the release ships, so the analysis scripts are
# CPython 3 and can be read by the same eyes as the rest of abi/.
VENV=$ROOT/ghidra/venv
if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install -q --no-index \
        --find-links "$DIR/Ghidra/Features/PyGhidra/pypkg/dist" pyghidra
fi
"$VENV/bin/python" -c "import pyghidra" || { echo "pyghidra install failed" >&2; exit 1; }
# The scripts that write names and bookmarks read abi/symbols.yaml and
# abi/words.yaml from inside the venv, so it needs the same yaml reader the
# rest of abi/ uses.
"$VENV/bin/python" -c "import yaml" 2>/dev/null || "$VENV/bin/pip" install -q pyyaml

echo "$DIR"
