#!/usr/bin/env bash
# Mirror the phone's HCI snoop log locally and decode WPP frames as they arrive.
#
# Usage: tools/follow_capture.sh [local-log] [--from-start] [--quiet]
#
# The snoop log lives on a root-adb readable path and only grows, so it is
# re-copied whole each second and wppdump prints whatever is new.
set -uo pipefail

REMOTE=/data/misc/bluetooth/logs/btsnoop_hci.log
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL="${1:-$REPO/btsnoop_hci.log}"
shift 2>/dev/null || true

if ! adb shell "test -r $REMOTE" 2>/dev/null; then
    echo "cannot read $REMOTE on the device" >&2
    echo "needs root adb and Developer options > Enable Bluetooth HCI snoop log" >&2
    exit 1
fi

cargo build --release --quiet --manifest-path "$REPO/wpp/Cargo.toml" || exit 1
WPPDUMP="$REPO/wpp/target/release/wppdump"

# Pull in the background so the decoder sees a file that grows.
(
    while true; do
        if adb exec-out cat "$REMOTE" > "$LOCAL.part" 2>/dev/null; then
            mv -f "$LOCAL.part" "$LOCAL"
        fi
        sleep 1
    done
) &
PULLER=$!
trap 'kill $PULLER 2>/dev/null; rm -f "$LOCAL.part"' EXIT INT TERM

# Wait for the first copy so the decoder does not open a missing file.
while [ ! -s "$LOCAL" ]; do sleep 0.2; done

"$WPPDUMP" "$LOCAL" --follow "$@"
