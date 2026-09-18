#!/usr/bin/env python3
"""The functions no static caller reaches, named from what a live run runs.

    python3 abi/trace.py             # writes the trace class of abi/symbols.yaml

`abi/survey.py` leaves 398 unnamed functions with no caller in the section
relocation graph. Two things put a function there and they are not the same
thing. One is a slot: a body whose address is only ever written into a RAM
structure at runtime -- an nrfx driver's event handler, a comparator handed to
a sort, a PWM instance's callback -- which no static cut can see because the
call goes through a word the running image wrote. The other is a private
helper reached by a `bl` that stays inside its own section, which needs no
relocation and so leaves no edge. Either way the image says nothing about the
body, and what is left to ask it is: under which feature does it run, and how
often against a rate that is already named.

So each row here carries a scenario and a rate. The scenarios are
`wpp-sim-client`'s, driven over the pipe the way the phone drives them:

    workout    --workout 120 with the ADXL367 given `Motion` every three
               seconds and the MAX86173 `Worn true` at 128 bpm
    hr         --hr-measure 90, worn and still at 128 bpm
    sleepmode  --sleep 120, worn and still at 52 bpm
    misc       --alarm --notify
    ecg        --ecg 90

each run against a copy of the sim with `calltrace.py --order --addresses`
hooks over the 398, the algorithm steps and the WUI slot bodies, the counters
zeroed once the firmware is up so a count is the scenario's and not the boot's.
A count is evidence about rate and never about role, so a `rate` line says the
ratio and the scenario and stops; the role comes from the body, by the same
shape rules abi/sensors.py reads. Where the two do not agree the address is
left out and the byte stays unnamed, which is the point of the refusals at the
end: of the 398, 68 ran under some scenario and most of those are the watch's
own infrastructure, which runs in every scenario at the same rate and is
therefore named by nothing here.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import symbols as symmap  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
GHIDRA = os.path.join(HERE, "out", "ghidra")
CLASS = "trace"

# address, name, kind, module, evidence; `calls` are callee addresses the body
# must still reach and `reads` are literal-pool values it must still load.
# `rate` is what the counting runs measured. `settled` is whether the rate is
# what fixes the name: where it is, the address goes in the verified set.
ROWS = [
    # --- the three step motors' nrfx PWM callbacks ---------------------------
    dict(address=0xA4F88, name="step_motor_pwm_finished_0", kind="function",
         module="hands", calls=[0x7E8F8], settled=True,
         rate="45, 23, 23 and 23 calls in the workout, hr, sleep and misc runs"
              " and none in the ecg one, against 401, 230, 230 and 108 of"
              " step_motor_play_step; the three thunks' counts sum to the"
              " moves their motors were asked for",
         evidence="twelve bytes that compare the nrfx PWM event type against 3"
                  " (NRFX_PWM_EVT_FINISHED) and tail-call step_motor_pwm_handler"
                  " with the constant 0, which is the motor index the handler"
                  " shifts into 0x20021b5c and indexes its tables by. The"
                  " address is only ever written into the nrfx PWM instance's"
                  " handler slot, which is why no static caller reaches it;"
                  " step_motor_pwm_handler's own entry already names this"
                  " address as one of its three thunks"),
    dict(address=0xA4F94, name="step_motor_pwm_finished_1", kind="function",
         module="hands", calls=[0x7E8F8], settled=True,
         rate="206, 117, 117, 55 and 109 calls across the five scenarios,"
              " against 401, 230, 230, 108 and 139 of step_motor_play_step",
         evidence="the same twelve bytes as step_motor_pwm_finished_0 with the"
                  " constant 1"),
    dict(address=0xA4F7C, name="step_motor_pwm_finished_2", kind="function",
         module="hands", calls=[0x7E8F8], settled=True,
         rate="150, 90, 90, 30 and 30 calls across the five scenarios, against"
              " 401, 230, 230, 108 and 139 of step_motor_play_step",
         evidence="the same twelve bytes as step_motor_pwm_finished_0 with the"
                  " constant 2"),

    # --- the SAADC the ECG front end runs on ---------------------------------
    dict(address=0x7E2A8, name="saadc_event_handler", kind="function",
         module="ecg", calls=[0x7DF40], settled=True,
         rate="67 calls in the ecg run and none in any other scenario, against"
              " the 32 live frames the measurement pushed",
         evidence="reads a function pointer out of the driver's context at"
                  " +0xcc and jumps to it with the event's own argument"
                  " (0x7e2b0..0x7e2b6), and on event type 3 flips a one-bit"
                  " buffer index and hands nrfx_saadc_buffer_set the other of"
                  " the two EasyDMA buffers. That is the nrfx SAADC event"
                  " handler, the slot DEVELOPMENT.md's ECG front end is driven"
                  " through; its address lives in the driver's control block,"
                  " so no static caller reaches it"),

    # --- the accelerometer chain under sensors_sync_push_accel ---------------
    dict(address=0xA1466, name="acc_iir4_i16_step", kind="function",
         module="sensors_sync", settled=True,
         rate="466 calls in the workout run against 9 in each of the hr, sleep"
              " and misc runs and 14 in the ecg one: it runs at the rate the"
              " wrist moves and at nothing else. In the four runs that are not"
              " the ECG one it is one for one with motion_energy_step; in the"
              " ECG run that body runs 137 times to its 14, so the two share"
              " an input and not a gate",
         evidence="a fourth-order direct-form-I fixed-point filter step: it"
                  " shifts the two int16 delay lines its arguments point at"
                  " back one place and writes the new input at +8"
                  " (0xa1466..0xa1488), then accumulates six smlabb terms with"
                  " the immediate coefficients -3913, 639, 17756, -1279,"
                  " -32767 and 28348 over the two lines plus 639 times the new"
                  " input, divides by 9480 and stores the result as the new"
                  " output (0xa148a..0xa14d4). Reached only from the split body"
                  " at 0xa1420, which sensors_sync_push_accel reaches through"
                  " 0xa446c, so it is a stage of the wrist-activity chain and"
                  " the motion run is what separates it from the still ones"),

    # --- the vibrator's own PWM callback -------------------------------------
    dict(address=0x7EEFC, name="vibrator_pwm_finished", kind="function",
         module="vib", calls=[0x4EBF0], reads=[0x20021B70],
         rate="2 calls in the workout run, at the moment its own step count"
              " crossed the daily goal and the log printed '[SCREEN_GOAL]"
              " Screen Goal Reached', and 4 in the ecg run around the"
              " measurement's start and end; none in the three scenarios that"
              " give the wearer nothing to feel",
         evidence="the same nrfx PWM callback shape as the step motors'"
                  " thunks -- event type against 3 -- but instead of stepping a"
                  " motor it clears 0x20021b70, gives 0x4ebf0 the handle 0x100"
                  " and, if that woke a task, sets PENDSVSET in SCB->ICSR at"
                  " 0xe000ed04 with the dsb and isb a yield from an interrupt"
                  " needs (0x7ef18..0x7ef28). It sits immediately before"
                  " 0x7ef38, which the map already holds as a vibrator body"),

    # --- the WPP channel predicates ------------------------------------------
    dict(address=0x9D456, name="wpp_opcode_is_master_request", kind="function",
         module="wpp",
         rate="64, 48, 12, 16 and 42 calls across the five scenarios, which is"
              " the frames each exchanged and not a role",
         evidence="`return opcode < 0x4000` over the whole 16-bit word, which"
                  " is the channel field wpp/src/frame.rs masks with 0xC000:"
                  " 0x0000 is MasterRequest and every other channel is above"
                  " it. Its address is taken and never called, which is what"
                  " leaves it with no caller"),
    dict(address=0x9D462, name="wpp_opcode_is_watch_originated", kind="function",
         module="wpp",
         rate="64 calls in the ecg run, two per live frame, and none in any"
              " other scenario",
         evidence="masks the opcode with 0xC000 and answers one for 0x4000 and"
                  " for 0x8000 and zero otherwise, which is"
                  " wpp/src/frame.rs's SlaveRequest and Notification: the two"
                  " channels the watch is the originator of",
         calls=[]),

    # --- a comparator handed to a sort ---------------------------------------
    dict(address=0xA0DBE, name="cmp_f32", kind="function",
         module="sensors_sync",
         rate="3 calls in the ecg run and none in any other scenario",
         evidence="loads a float through each of its two pointer arguments,"
                  " compares them and returns 0, -1 or 1 (0xa0dbe..0xa0de4)."
                  " That is a C comparator, and a comparator is passed as a"
                  " pointer, which is the whole reason nothing calls it"),
]

# What ran and is still not named. Each carries the rate, because the rate is
# the measurement and it survives the refusal: a later run that starts the
# feature these belong to compares against it.
REFUSED = [
    (0x68400, "76993, 66423, 66416, 12656 and 61967 calls across the five"
              " scenarios: the most-called body in every one of them, a five"
              " argument wrapper around 0x9ce4a. A rate that is the same in"
              " every scenario names nothing"),
    (0x73094, "the first hook to fire in every scenario and 2485 calls in the"
              " workout: a two-instruction load of one word of 0x20021920,"
              " with forty-six static callers the relocation lift does not see"
              " because the calls stay inside one section"),
    (0x936B2, "967, 863, 914, 254 and 769 calls: a two-instruction tail call into"
              " 0x39ea4 with a zero second argument, which is a default"
              " argument wrapper and not a stage"),
    (0x977C6, "138 calls in each of the workout, hr and sleep runs, 207 in"
              " the misc one and none in the ecg one, which is a rate tied to"
              " nothing the scenarios varied"),
    (0x520F0, "48, 39, 63, 2 and 2 calls against 475 and 385 of"
              " algo_dispatch_sample: about a tenth of the sample rate in"
              " three scenarios and almost nothing in the other two, which is"
              " a periodic task and not an algorithm stage"),
    (0x6C204, "32 calls in the ecg run only, one per live frame, over a body"
              " that masks fourteen bits off its third argument and compares"
              " the result against 0x13d, 0x13e, 0x140 and 0x968 -- the"
              " workout commands and a cache type. One scenario and a"
              " command-id filter do not agree, so it is left alone"),
    (0x2F3F8, "32 calls in the ecg run only, over a body that writes its"
              " second argument into a global once and never again: a"
              " registration whose global is 0x2001714c and whose owner the"
              " trace does not name"),
    (0x2F860, "one call at the very end of the workout run and nowhere else,"
              " but the partition cuts it as 0x2f860..0x5cab4, so the body the"
              " hook fired on is not the body the entry would name"),
    (0x91F4C, "one call in the sleep run and nowhere else, which is one"
              " sample of one scenario"),
]


def load_analysis():
    with open(os.path.join(GHIDRA, "items.json")) as fh:
        items = json.load(fh)
    with open(os.path.join(GHIDRA, "references.json")) as fh:
        refs = json.load(fh)
    with open(os.path.join(GHIDRA, "modules.json")) as fh:
        modules = json.load(fh)["functions"]
    starts = dict((f["start"], f) for f in items["functions"])
    calls, pool = {}, {}
    for call in refs["calls"]:
        if call.get("function") is None:
            continue
        calls.setdefault(call["function"], set()).add(call["to"] & ~1)
    for word in refs["words"]:
        for reader in word["readers"]:
            pool.setdefault(reader, set()).add(word["value"])
    return starts, calls, pool, modules


def checked(rows):
    """Every row, with the claims it makes about the image still true.

    A name is worth no more than the reading behind it, so the callees and the
    pool words the evidence cites are re-measured against the analysis this run
    reads. A body that no longer calls what the reading says it calls is a
    different body, and naming it would be the stale claim winning.
    """
    starts, calls, pool, modules = load_analysis()
    partition = dict((a, v["module"]) for a, v in modules.items())
    out, complaints = [], []
    for row in rows:
        at = row["address"]
        fn = starts.get(at)
        if fn is None:
            complaints.append("0x%x is not a function start" % at)
            continue
        made = calls.get(at, set())
        missing = [c for c in row.get("calls", ()) if c not in made]
        if missing:
            complaints.append("%s at 0x%x no longer calls %s"
                              % (row["name"], at,
                                 ", ".join("0x%x" % c for c in missing)))
            continue
        read = set()
        for site in range(at, fn["end"]):
            read |= pool.get(site, set())
        absent = [v for v in row.get("reads", ()) if v not in read]
        if absent:
            complaints.append("%s at 0x%x no longer loads %s"
                              % (row["name"], at,
                                 ", ".join("0x%x" % v for v in absent)))
            continue
        out.append(dict(address=at, name=row["name"], kind=row["kind"],
                        **{"class": CLASS,
                           "module": partition.get("0x%x" % at,
                                                   row.get("module")),
                           "evidence": row["evidence"] + ". " + row["rate"]}))
    return out, complaints


def main():
    rows, complaints = checked(ROWS)
    for line in complaints:
        print("abi/trace.py: %s" % line, file=sys.stderr)
    if complaints:
        sys.exit("abi/trace.py: %d reading(s) no longer hold" % len(complaints))
    settled = set(r["address"] for r in ROWS if r.get("settled"))
    try:
        added = symmap.load().rewrite(rows, {CLASS}, verified=settled)
    except symmap.Refusal as err:
        sys.exit("abi/trace.py: abi/symbols.yaml: %s" % err)
    print("%d names from the scenario traces checked against the analysis;"
          " %d entries added, %d of them settled by their rate"
          % (len(rows), added, len(settled)))
    print("%d addresses refused:" % len(REFUSED))
    for at, why in REFUSED:
        print("  0x%x: %s" % (at, " ".join(why.split())))


if __name__ == "__main__":
    main()
