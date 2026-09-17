#!/bin/bash
# Stage a rig whose scripts name the addresses a moved layout put things at.
#
#   abi/moved_rig.sh shift|reverse        # -> out/moved-rig, for the update test
#
# The update run starts from the stock image and only runs the moved one after
# the bootloader has installed it, so machine.resc keeps the original addresses
# and only the fixups that follow the update are rewritten: moved-patches.resc's
# hook addresses, plus a redefinition of imagePatches that takes effect after
# machine.resc has already applied the stock one.
#
#   UPDATE_TEST_RIG=renode-sim/out/moved-rig LAYOUT=reverse \
#       cargo test -p wpp-sim-client --test update -- --test-threads=1
set -eu
cd "$(dirname "$0")"
LAYOUT=${1:?usage: moved_rig.sh shift|reverse}
SIM=..
RIG=$SIM/out/moved-rig

LAYOUT="$LAYOUT" ./relink.sh > /dev/null
rm -rf "$RIG"
mkdir -p "$RIG"
for path in "$SIM"/*; do
    name=$(basename "$path")
    [ "$name" = out ] && continue
    case "$name" in
        *.resc) cp "$path" "$RIG/$name" ;;
        *) ln -s "$(cd "$(dirname "$path")" && pwd)/$name" "$RIG/$name" ;;
    esac
done
python3 sim_patches.py "$SIM/moved-patches.resc" "$RIG/moved-patches.resc"
python3 sim_patches.py "$SIM/machine.resc" "$RIG/image-patches.resc" --macro imagePatches
cat "$RIG/image-patches.resc" >> "$RIG/moved-patches.resc"
rm "$RIG/image-patches.resc"
echo "$RIG staged for the $LAYOUT layout"
