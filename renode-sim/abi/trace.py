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
The hooks cost about a sevenfold slowdown, so the 120 s workout is 18.6 s of
watch time and not two minutes of it. Wall time does not buy that back: the
same scenario run for 900 s reached the same 104 bodies, address for address,
over 10.8 s of watch time, because a contended host is what sets the pace. A
window an algorithm needs minutes to close will not close by running the
client longer; it needs the algorithm started.

abi/algos.py adds the scenarios those five leave out, over the same hook set,
because the ten algorithms the registry at 0xb48dc lists are started by nothing
the phone's ordinary traffic asks for:

    ppgmodes    the debug console driven for the first time -- the shell only
                exists when P0.08 idles high (the cable check at 0x2e5e8) --
                with `max8617x start` in each of the thirteen measurement modes
                the registry's rows index, and then `max8617x test`
    bodytemp    `greenteg test` and `body_temperature kickstart`, which is the
                core-body-temperature algorithm run over a simulated week
    spo2        MEASURE_START with category 3, which the handler's switch at
                0x54028 admits and which does start the sample pipeline
    ppgmeasure  MEASURE_START with category 2, which the same switch refuses
    bodytempwpp the skin-temperature, heat-flux and greenTEG commands
    rawdatawpp  RAW_DATA with the mark-synced command
    schedule    twenty minutes of watch time with nobody talking to it

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
         rate="exactly 200 calls in the run that typed `tracker_algo"
              " sleep_wake 200` at the console, which is one per iteration the"
              " shell asked for and names the algorithm it belongs to outright:"
              " the command's body at 0x64158 compares argv[0] against"
              " \"sleep_wake\" and calls 0xa1420 that many times, and nothing"
              " else in that run entered this body. Before the console could be"
              " driven the reading was a ratio instead: 466 calls in the workout"
              " run against 9 in each of the hr, sleep and misc runs and 14 in"
              " the ecg one, one for one with motion_energy_step in the four"
              " runs that are not the ECG one, where that body runs 137 times"
              " to its 14, so the two share an input and not a gate",
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

    # --- the debug console's own input path ----------------------------------
    dict(address=0x59BE4, name="shell_uart_rx_byte", kind="function",
         module="shell", calls=[0x9E7AC], reads=[0x2001F8E4], settled=True,
         rate="655 calls in the run that typed 655 characters at the console"
              " and 107 in the one that typed 107 before a command took the"
              " console back, and none in the four that leave P0.08 low. One"
              " call per character received is the whole rate",
         evidence="the byte the UART0 interrupt handler at 0x3b3ec read out of"
                  " RXD, pushed into the console's character queue: it loads"
                  " the shell context from 0x2001f8e4, takes the queue handle"
                  " at +0xb8 and calls xQueueGenericSendFromISR (0x9e7ac) with"
                  " a pointer to the byte and a woken flag, then sets PENDSVSET"
                  " in SCB->ICSR when the send woke the reader. Its address is"
                  " written into the driver's callback slot 0x2001828c by"
                  " 0x59cc4, which is why no static caller reaches it"),

    # --- the optical front end's watchdog ------------------------------------
    dict(address=0x52498, name="max8617x_swdog_feed", kind="function",
         module="max8617x", calls=[0x5E7A8, 0x5DF94], reads=[0xD9B4D],
         settled=True,
         rate="7384 calls in the run that started the front end in each of its"
              " thirteen measurement modes, against 57806 of"
              " ppg_sample_channel_slot in the same run, and none in any of"
              " the other five",
         evidence="eighteen bytes that log the format at 0xd9b4d,"
                  " '[max8617x] [%d] %d', through wlog_fmt_r0d and then tail"
                  " call swdog_feed (0x5df94) with the constant 6. Six is the"
                  " software watchdog the front end takes out for itself: the"
                  " console prints '[SWDOG] enable swdog #6, timeo=10000ms' on"
                  " every `max8617x start` and '[SWDOG] disable swdog #6' on"
                  " every stop, so the body is the sample path saying the"
                  " front end is still delivering"),

    # --- the ten-minute sweep that starts the passive measurements ----------
    dict(address=0x3FC14, name="periodic_task_sweep", kind="function",
         module="misc", calls=[0x3A85C, 0x946B2], reads=[0x27D64, 0x27D94],
         settled=True,
         rate="9 calls in the twenty-minute schedule run and none in any of"
              " the other eight, against 9 of the third row's handler"
              " 0x62410 in the same run: every sweep that found that row due"
              " ran it, which is what a sweep over a due list does",
         evidence="the walk over periodic_task_table: it takes the uptime"
                  " through uptime_seconds (0x3a85c) and steps the twelve-byte"
                  " rows from the pool word 0x27d64 to the pool word 0x27d94"
                  " (the `adds r4, #0xc` at 0x3fc58), and for each row whose"
                  " state block holds a non-zero deadline the uptime has"
                  " passed and whose enable byte at +4 is set, clears the"
                  " deadline and calls the row's handler through `blx r3`"
                  " (0x3fc30..0x3fc4a). A handler that answers -1 is re-armed"
                  " through 0x946b2 with the deadline it wrote back"
                  " (0x3fc4c..0x3fc54). This is the body the passive"
                  " measurements hang off, and nothing reaches it statically"
                  " because the rows it calls are words of a table no"
                  " relocation names"),

    # --- the activation functions of a small network -------------------------
    dict(address=0xA7542, name="nn_activation_logistic", kind="function",
         module="body_temp", calls=[0x8C9DC], settled=True,
         rate="376 calls in the body-temperature run, two for each of the 188"
              " of the two bodies beside it, and none in any of the other five",
         evidence="for each of the n floats its pointer argument walks it"
                  " writes 1 / (expf(-x) + 1): vldr the element, vneg, expf"
                  " (0x8c9dc), add the 1.0 held in s16 since the prologue and"
                  " vdiv that 1.0 by the sum (0xa755c..0xa7570). That is the"
                  " logistic function over a vector and nothing else is. Its"
                  " address is the third of three consecutive words at"
                  " 0xf0c8c, beside 0xa7577 and 0xa7541, and nothing in the"
                  " image references that trio: an activation table a model"
                  " descriptor indexes, which is why no static caller reaches"
                  " any of the three"),
    dict(address=0xA7576, name="nn_activation_elu", kind="function",
         module="body_temp", calls=[0x8CACC], settled=True,
         rate="188 calls in the body-temperature run and none in any of the"
              " other five",
         evidence="the same vector walk with a different rule: an element"
                  " already above zero is left alone and every other one is"
                  " replaced by expm1f of itself (the vcmpe against zero at"
                  " 0xa7588 and the call to 0x8cacc). x for x > 0 and"
                  " exp(x) - 1 otherwise is the exponential linear unit at"
                  " alpha one. It is the first of the three words at 0xf0c8c"),
    dict(address=0xA7540, name="nn_activation_identity", kind="function",
         module="body_temp",
         rate="188 calls in the body-temperature run, one for each of"
              " nn_activation_elu, and none in any of the other five",
         evidence="two bytes, `bx lr`. On its own that names nothing; what"
                  " names it is where the two bytes sit: the word at 0xf0c90"
                  " holds 0xa7541 between the words holding nn_activation_elu"
                  " and nn_activation_logistic, and what follows them at"
                  " 0xf0c98 is greenteg_cbta_nn_tensors, the five weight"
                  " tensors of the core-body-temperature model."
                  " A row of an activation table that does nothing to its"
                  " vector is the linear activation, and a layer declared with"
                  " one is a layer with no non-linearity"),

    # --- the kernels that network is made of ---------------------------------
    # The bodies below have static callers, so they are not in the uncalled
    # set; what puts them here is the same thing that puts the activations
    # here, which is that a rate is the only evidence tying them to the
    # feature. They are shared kernels with no string, no log line and no
    # object of their own, and only the body-temperature run enters them.
    dict(address=0xA759E, name="nn_dense_accumulate", kind="function",
         module="body_temp", calls=[0xA86C2],
         rate="1057 calls in the body-temperature run against 151 of"
              " nn_gru_cell and 151 of nn_dense_layer, which is seven"
              " per network run -- six inside the cell and one in the"
              " output layer -- and none in any of the other five",
         evidence="out[m][n] = bias[n] + sum over k of in[m][k] *"
                  " w[k * n_out + n], in float32 and with `vfma.f32` so the"
                  " product is never rounded before it is added. It memsets"
                  " the output first (0xa75c0), walks the weight pointer in"
                  " strides of n_out floats over the summed dimension"
                  " (0xa7610..0xa7618) and only then adds the bias vector"
                  " element for the output it just finished (0xa762a). A"
                  " row-major matrix multiply with a per-output bias is a"
                  " dense layer's accumulation and nothing else",
         settled=True),
    dict(address=0xA76AA, name="nn_dense_layer", kind="function",
         module="body_temp", calls=[0xA759E],
         rate="151 calls in the body-temperature run, one for each entry"
              " of greenteg_cbta_network_run and one for each of"
              " nn_activation_identity, and none in any of the other"
              " five",
         evidence="the dense layer around that kernel: it takes an output, an"
                  " input, a weight and a bias descriptor of the shape"
                  " greenteg_cbta_nn_tensors declares, reads the batch from"
                  " the input's rank and rows, the output width from the"
                  " weight's cols at +0xa and the summed width from its rows"
                  " at +8, calls nn_dense_accumulate and tail-calls the"
                  " activation its fifth argument names over batch * cols"
                  " floats (0xa76e8)",
         settled=True),
    dict(address=0xA76EA, name="nn_gru_cell", kind="function",
         module="body_temp", calls=[0xA759E],
         rate="151 calls in the body-temperature run, one for each of"
              " nn_activation_elu and half of nn_activation_logistic's"
              " 302, and none in any of the other five",
         evidence="one timestep of a gated recurrent unit. It divides its"
                  " input weight's row count by three (0xa7702) and runs"
                  " nn_dense_accumulate six times, three blocks against the"
                  " input and three against the hidden state, each with its"
                  " own bias vector out of the six the bias tensor holds; it"
                  " sums the first two pairs and puts the logistic over them"
                  " (0xa77c4), multiplies the second gate into the third"
                  " recurrent projection and the ELU over that sum"
                  " (0xa7876, 0xa7858), and closes with h = fma(z, h,"
                  " (1 - z) * n) at 0xa78c4. Update gate, reset gate and"
                  " candidate, in that order, with the candidate's"
                  " non-linearity an ELU where a stock GRU has tanh",
         settled=True),
    dict(address=0xA78CE, name="nn_gru_run", kind="function",
         module="body_temp", calls=[0xA76EA],
         rate="151 calls in the body-temperature run, one for each of"
              " nn_gru_cell, and none in any of the other five: the"
              " model is run one timestep at a time",
         evidence="the sequence loop over that cell: it takes the hidden"
                  " width from its recurrent weight's cols, the input width"
                  " and the timestep count from the input tensor's cols and"
                  " rows, and steps the cell once per timestep forwards or"
                  " backwards depending on its fifth stack argument"
                  " (0xa78f2), copying the hidden state into the output"
                  " tensor after each step when the sixth says to. The CBTA"
                  " model passes one timestep, forwards, with the states"
                  " returned",
         settled=True),
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
    (0x9443C, "a strict-less predicate over the word at +8 of the two records"
              " its pointer arguments name, which is a comparator and has no"
              " caller for the usual reason. 29 calls in every one of the four"
              " scenarios that open the pipe and none in the two console ones,"
              " so the rate is the connection and says nothing about which"
              " feature sorts with it"),
    (0x807A4, "281, 166, 60 and 47 calls across the SpO2, PPG, temperature and"
              " raw-data runs and none in the two console ones: a state"
              " machine that calls two members of its object's own vtable at"
              " +0x34 and +0x38 and retries. Four scenarios and a vtable do"
              " not agree on a feature"),
    (0x9B2A4, "2195 calls in the body-temperature run and none in any other,"
              " over two instructions that store a halfword at +6 of their"
              " first argument. A setter whose owner the trace does not name"),
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
