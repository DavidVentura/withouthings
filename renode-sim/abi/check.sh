#!/bin/bash
# Run the cheap proofs over a set of relink configurations and report a table.
#
#   abi/check.sh                 # every configuration
#   abi/check.sh pack gc         # only the named ones, plus the baseline
#
# Four proofs per configuration, in the order that makes the later ones worth
# running: the link itself; the identity link byte-identical to the stock app
# image, which says the cut and the relocations are right before anything else
# is asked; abi/stale_scan.py with nothing unexplained, which says no reference
# stayed behind when a section moved or was dropped; and 60 s of the display run
# with a zero-line log diff.
#
# The baseline for that diff is the plain relink and not stock, so that a
# configuration is measured against the relink it is a variation of. The two are
# now the same run: once the reference config reached the image's own heap size,
# task-name length and strict aliasing, the plain relink's 60 s log is stock's
# line for line and its tick is one above stock's, 59536 against 59535, which is
# where the two tickless-idle implementations round the last suppressed tick.
#
# The update test and the ECG run are not here: they need fixed ports and
# minutes of virtual time, so they stay separate runs.
# No globbing: one configuration's SPILL is a linker input pattern and the
# shell must not try to match it against this directory.
set -uf
cd "$(dirname "$0")"
SIM=$(cd ..; pwd)
RIGS=${RIGS:-/tmp/claude-1000/fixlink}
RENODE=${RENODE:-$(command -v renode || echo "$HOME/renode-portable/renode")}

# name:environment. The plain relink is first because every other diff is
# against its log.
CONFIGS=(
    "plain:"
    "gc:GC=1"
    "newlib-data:REPLACE=newlib DATA=1"
    "pack:GC=1 REPLACE=newlib,libm LAYOUT=pack DATA=1 SPILL=*libm.a:"
    "prune-tunnel:GC=1 PRUNE=wpps_tls_tunnel DATA=1"
    "get-fw-version:REPLACE=get_fw_version"
)

# The plain relink runs whatever the arguments say, because it is the log every
# other configuration is diffed against.
wanted() {
    [ $# -eq 0 ] && return 0
    [ "$WANT" = plain ] && return 0
    for name in "$@"; do [ "$name" = "$WANT" ] && return 0; done
    return 1
}

# A Renode run needs a directory of its own: two instances in one directory
# overwrite each other's out/uart0.log. Every regular file of renode-sim/ is
# symlinked in, plus the scripts/ and models/ directories whole, so the scripts,
# the platform, the peripheral models and the two flash images are the repo's
# own and only out/ is per-run. Renode resolves every `@path` against the
# working directory rather than the including file, so the scratch copy has to
# carry the same directory shape as renode-sim/ and not a flattened one.
rig_dir() {
    local name=$1 image=$2 symbols=$3
    local dir=$RIGS/$name
    rm -rf "$dir"; mkdir -p "$dir/out/frames"
    local f
    set +f
    for f in "$SIM"/*; do
        [ -f "$f" ] && ln -s "$f" "$dir/$(basename "$f")"
    done
    set -f
    ln -s "$SIM/scripts" "$dir/scripts"
    ln -s "$SIM/models" "$dir/models"
    cp "$image" "$dir/out/flash-relinked.bin"
    python3 rig.py --image "$image" --symbols "$symbols" \
        --out "$dir/out/rig" > "$dir/out/rig.log" 2>&1 || return 1
    echo "$dir"
}

display_run() {
    local dir=$1
    (cd "$dir" && "$RENODE" --disable-xwt --console \
        -e '$image=@out/flash-relinked.bin' -e "include @scripts/display-run.resc" \
        > out/renode.log 2>&1 < <(sleep 600))
    grep -q "=== display-run done ===" "$dir/out/renode.log"
}

pass=0
fail=0
rows=()
BASELINE=""
for entry in "${CONFIGS[@]}"; do
    WANT=${entry%%:*}
    env_line=${entry#*:}
    wanted "$@" || continue
    start=$SECONDS
    log=$SIM/out/check-$WANT.log
    verdict=""

    if ! (cd "$SIM" && env $env_line abi/relink.sh) > "$log" 2>&1; then
        verdict="link failed"
    elif ! grep -q "^byte-identical:" "$log"; then
        verdict="identity not byte-identical"
    fi

    if [ -z "$verdict" ]; then
        scan=$SIM/out/check-$WANT-stale.log
        python3 stale_scan.py --elf "$SIM/out/relink/relinked.elf" \
            --dropped "$SIM/out/relink/relinked.gc" \
            --map "$SIM/out/relink/relinked.map" > "$scan" 2>&1
        left=$(sed -n 's/^\([0-9]*\) survivors are not explained$/\1/p' "$scan" | tail -1)
        if [ "${left:-none}" != 0 ]; then
            verdict="stale scan: ${left:-no verdict} unexplained"
        fi
    fi

    if [ -z "$verdict" ]; then
        dir=$(rig_dir "$WANT" "$SIM/out/flash-relinked.bin" \
                      "$SIM/out/relink/relinked.elf")
        if [ -z "$dir" ]; then
            verdict="rig refused the image"
        elif ! display_run "$dir"; then
            verdict="display run did not finish"
        elif [ -z "$BASELINE" ]; then
            BASELINE=$dir/out/uart0.log
            verdict="ok (baseline)"
        else
            lines=$(diff "$BASELINE" "$dir/out/uart0.log" | grep -c '^[<>]')
            [ "$lines" = 0 ] && verdict="ok" || verdict="$lines log lines differ"
        fi
    fi

    took=$((SECONDS - start))
    case $verdict in ok*) pass=$((pass + 1)) ;; *) fail=$((fail + 1)) ;; esac
    rows+=("$(printf '%-16s %-58s %4ds  %s' "$WANT" "${env_line:-(no environment)}" "$took" "$verdict")")
done

echo
echo "configuration    environment                                                time  verdict"
for row in "${rows[@]}"; do echo "$row"; done
echo
echo "$pass passed, $fail failed"
[ "$fail" = 0 ]
