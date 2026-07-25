#!/usr/bin/env bash
# Rebuild tools/wpp.json from a Withings APK.
#
# Usage: tools/decompile.sh <withings.apk> [jadx-dir]
#
# Only needed when Withings ships new object types; wpp.json is committed, so
# regenerating the Rust normally just means:
#     python3 tools/emit_rust.py tools/wpp.json -o wpp/src
#
# The APK from the Play Store is an app bundle: the base APK holds the dex, and
# the native libraries live in split_config.<abi>.apk. Only the dex is needed
# here. To grab both off a connected phone:
#     adb shell pm path com.withings.wiscale2
#     adb exec-out cat <path> > base.apk
set -euo pipefail

APK="${1:-}"
JADX="${2:-jadx}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if [ -z "$APK" ] || [ ! -f "$APK" ]; then
    echo "usage: tools/decompile.sh <withings.apk> [jadx-dir]" >&2
    exit 1
fi

if ! command -v "$JADX" >/dev/null 2>&1; then
    echo "jadx not found (pass its path as the second argument)" >&2
    echo "get it from https://github.com/skylot/jadx/releases" >&2
    exit 1
fi

# The wpp classes are spread across several dex files; decompiling only those
# that mention the package keeps this to a couple of minutes.
echo "== extracting dex from $APK"
unzip -o -q "$APK" 'classes*.dex' -d "$WORK/dex"

# grep -a rather than `strings | grep -q`: under `set -o pipefail` an early
# grep exit makes strings fail on SIGPIPE and the test always reads false.
DEX=()
for f in "$WORK"/dex/*.dex; do
    if grep -qa "Lcom/withings/comm/wpp/generated/" "$f"; then
        DEX+=("$f")
    fi
done
if [ ${#DEX[@]} -eq 0 ]; then
    echo "no dex contains com/withings/comm/wpp/generated — wrong APK?" >&2
    exit 1
fi
echo "== decompiling ${#DEX[@]} of $(ls "$WORK"/dex/*.dex | wc -l) dex files"

# jadx reports errors on unrelated classes; the wpp ones decompile cleanly, and
# extract_wpp.py fails loudly if anything it needs is missing.
JAVA_OPTS="-Xmx8g" "$JADX" -j "$(nproc)" --no-res --no-imports \
    -d "$WORK/out" "${DEX[@]}" >/dev/null 2>&1 || true

SRC="$WORK/out/sources"
if [ ! -d "$SRC/com/withings/comm/wpp/generated" ]; then
    echo "decompilation produced no wpp package" >&2
    exit 1
fi

echo "== extracting protocol description"
python3 "$REPO/tools/extract_wpp.py" "$SRC" -o "$REPO/tools/wpp.json"

echo
echo "wrote $REPO/tools/wpp.json"
echo "next: python3 tools/emit_rust.py tools/wpp.json -o wpp/src && cargo test --manifest-path wpp/Cargo.toml"
