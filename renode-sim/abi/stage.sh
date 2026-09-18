#!/bin/bash
# Stage the FreeRTOS this firmware was built from.
#
#   abi/stage.sh <dest>
#
# <dest>/source and <dest>/portable are the SDK's kernel and port with
# abi/patches/*.patch and abi/patches/freertos/*.patch applied -- the changes
# Withings made, recovered from the image's own bytes -- and abi/facts.yaml's
# reference_build corrections written into the headers they correct.
#
# abi/refbuild.sh measures a build of this tree against the image and
# abi/relink.sh links one into the firmware, so what is measured is what runs.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=${ROOT:-$HOME/ref-build}
SDK=$ROOT/sdk/nRF5_SDK_17.1.0_ddde560
dest=$1

rm -rf "$dest"
mkdir -p "$dest"
cp -r "$SDK/external/freertos/source" "$dest/source"
cp -r "$SDK/external/freertos/portable" "$dest/portable"
# The SDK sources are CRLF and so are the patches.
for p in "$HERE"/patches/*.patch; do
    [ -f "$p" ] || continue
    patch -s -d "$dest/source" -p1 < "$p"
done
for p in "$HERE"/patches/freertos/*.patch; do
    [ -f "$p" ] || continue
    patch -s -d "$dest/portable" -p1 < "$p"
done
python3 "$HERE/refbuild_fix.py" --port "$dest/portable" > /dev/null
