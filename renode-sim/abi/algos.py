#!/usr/bin/env python3
"""Start the algorithms no driven path starts, and count what runs.

    python3 abi/algos.py --emit                 # the scratch copy and its scripts
    python3 abi/algos.py --run [--scenario N]   # one scenario, or every one
    python3 abi/algos.py --report               # the counts, per scenario

abi/trace.py's five scenarios leave most of the functions the section graph
gives no caller unexecuted, because the algorithms those functions belong to
are started by nothing the phone's ordinary traffic asks for. The registry at
0xb48dc names eighteen of them -- HR_LONG_MEASURE, HR_BURST, HR_SPECTROTRACK,
HR_TEMPO, SPO2_MULTI, SPO2_MONO, PPG_ARRHYTHMIA, PPG_HRV, MOTION_DETECTION,
WORN, PPG_BR, PPG_APNEA, PPG_HEART_BEATS, SLEEP_WAKE, RAW_DATA_MODE_ACC_PPG,
PPG_RR, LIBDBT -- and each carries the optical front end's measurement mode it
needs. Nothing turns that front end on by itself, so nothing runs.

The three ways in are the three kinds of scenario here:

    the debug shell, which the sim did not drive until this. The console is
    UART0 and its reader is gated on the debug cable: 0x2e5e8 reads P0.08 and
    only enters shell_init when the line idles high, so `sysbus.gpio0 OnGPIO 8
    true` is what makes the watch have a shell at all. The reader's queue holds
    eight bytes, so a line is typed a character at a time;

    a WPP command the existing client never sends -- MEASURE_START with a
    category other than ECG, the body-temperature commands, RAW_DATA;

    and time, for what runs on a schedule rather than on a request.

Every scenario carries the same hook set: abi/tracepoints.py's addresses under
calltrace.py's `--order` hooks, which count and keep the position and the
virtual time of the first entry. A count is evidence about rate and never about
role, which is why this writes counts and abi/trace.py writes names.
"""

import argparse
import collections
import os
import shutil
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import observe_calls as oc  # noqa: E402
import symbols as symmap  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
ROOT = os.path.dirname(SIM)
RUNTIME = os.path.join(SIM, "out", "algos", "rt")
MONITOR_PORT = 12400

# The counts live on the `sys` module because each hook gets its own Python
# scope; the dump is a file the monitor executes rather than a monitor line,
# since a monitor `python` line is one line and this is not.
DUMP = """import sys
c = getattr(sys, 'calltrace', dict())
f = open(%(path)r, 'w')
for k in sorted(c):
    e = c[k]
    f.write('%%x %%d %%d %%.0f\\n' %% (k, e[0], e[1], e[2]))
f.close()
print 'DUMPED %%d addresses' %% len(c)
"""

# The optical front end's measurement modes, as `max8617x list_modes` prints
# them. The registry's +0xd byte is an index into this list, which is what ties
# an algorithm to the mode it needs.
PPG_MODES = ["MULTIPPG", "MULTIPPG_LONG_MEASURE", "MULTIPPG_MONO",
             "MULTIPPG_MONO_LONG_MEASURE", "MULTIPPG_GREEN",
             "MULTIPPG_GREEN_LONG_MEASURE", "SINGLEPPG_GREEN",
             "SINGLEPPG_GREEN_ECG_MEASURE", "IR", "IR940_NORTH", "GREYCARD",
             "DARK", "CROSSTALK"]


class Scenario(object):
    """One run: how the watch is set up, and what drives it from outside."""

    def __init__(self, name, why, setup=(), body=(), client=(), shell=(),
                 motion=False, settle=10):
        self.name = name
        self.why = why
        self.setup = list(setup)
        self.body = list(body)
        self.client = list(client)
        # (line, seconds to let the watch run afterwards) pairs, typed at the
        # console a character at a time.
        self.shell = list(shell)
        self.motion = motion
        self.settle = settle

    @property
    def pipe(self):
        return bool(self.client)


WORN = ['spi1.max86173 Worn true', 'spi1.max86173 HeartRateBpm 128']
RESTING = ['spi1.max86173 Worn true', 'spi1.max86173 HeartRateBpm 52']
# The two sensors the core-body-temperature algorithm integrates: the skin
# thermometer and the heat-flux bridge. Left at their defaults the algorithm
# reads zero and refuses every sample with "temperature 0 out of range".
SKIN = ['twi0.tmp117 TemperatureCelsius 34.5',
        'twi0.ads1115 InputMicrovolts 420.0']
# The body-temperature task refuses to start on a charger: the gate at 0x42888
# reads the charger through 0x922a2 and forces its go byte to zero, which is
# the -1 `body_temperature start` answers with and prints nothing about.
OFF_CHARGER = ['twi0.bq25180 VinPresent false', 'twi0.bq25180 Charging false']


def ppg_mode_lines():
    lines = []
    for mode in PPG_MODES:
        lines.append(("max8617x start %s" % mode, 20))
        lines.append(("max8617x stop", 5))
    lines.append(("max8617x release", 5))
    return lines


SCENARIOS = collections.OrderedDict(
    (s.name, s) for s in [
        Scenario("ppgmodes",
                 "the optical front end run in every measurement mode the"
                 " registry's algorithms ask for",
                 setup=WORN,
                 shell=[("max8617x list_modes", 3), ("max8617x request", 5)]
                       + ppg_mode_lines()
                       + [("max8617x test 30 MULTIPPG", 60)]),
        Scenario("bodytemp",
                 "the core-body-temperature algorithm, with the skin and"
                 " heat-flux sensors reading a wrist",
                 setup=WORN + SKIN + OFF_CHARGER,
                 shell=[("body_temperature init", 5),
                        ("greenteg config get", 5),
                        ("body_temperature start", 60),
                        ("greenteg test", 60),
                        ("body_temperature kickstart", 240),
                        ("body_temperature dump", 10),
                        ("body_temperature recover", 20),
                        ("body_temperature stop", 5)]),
        Scenario("rawdata",
                 "the raw-data capture, with the accelerometer and the optical"
                 " front end both feeding it",
                 setup=WORN, motion=True,
                 shell=[("raw_data enable", 5),
                        ("max8617x start MULTIPPG", 60),
                        ("raw_data size_get 0", 5),
                        ("max8617x stop", 5),
                        ("raw_data set 1 0 8 0", 10),
                        ("raw_data set 6 0 8 0", 10),
                        ("raw_data du", 5),
                        ("raw_data flush", 10),
                        ("raw_data disable", 5)]),
        Scenario("tracker",
                 "the activity trackers and the background sync",
                 setup=WORN, motion=True,
                 shell=[("tracker_live init", 5),
                        ("tracker_live data", 5),
                        ("vasistas steps", 5),
                        ("vasistas num 1", 5),
                        ("background_sync sync_request", 15),
                        ("tracker_algo sleep_wake 200", 60),
                        ("tracker_algo tracker_activity 20", 20),
                        ("tracker_algo acti_worn 20", 20)]),
        Scenario("spo2", "MEASURE_START with the SpO2 category",
                 setup=WORN, client=["--spo2", "90"]),
        Scenario("ppgmeasure", "MEASURE_START with the PPG category",
                 setup=WORN, client=["--ppg", "30"]),
        Scenario("bodytempwpp", "the temperature commands the phone can send",
                 setup=WORN + SKIN, client=["--body-temp"]),
        Scenario("rawdatawpp", "the raw-data command the phone can send",
                 setup=WORN, client=["--raw-data"]),
        Scenario("schedule",
                 "twenty minutes of watch time, worn and at rest, with nobody"
                 " talking to it: what runs on a schedule and on nothing else."
                 " Twenty because the passive heart rate's cadence is ten"
                 " minutes, so the window holds two of them. The wrist is"
                 " shaken through the run because the burst that fires at ten"
                 " minutes waits on the worn tracker -- the log says"
                 " '[AUTO_BURST][  HR_MEASURE] wait for tracker_worn:"
                 " is_ready=0, is_worn=1' -- and that detector is only ready"
                 " once the accelerometer has given it something",
                 setup=RESTING + SKIN, motion=True,
                 body=['emulation RunFor "1200.0"']),
    ])


def hook_block(addresses_file):
    """calltrace.py's ordering hooks over the addresses a trace was asked for."""
    return subprocess.check_output(
        [sys.executable, os.path.join(SIM, "calltrace.py"), "--order",
         "--addresses", addresses_file],
        stderr=subprocess.DEVNULL).decode()


def tracepoints(runtime):
    path = os.path.join(runtime, "out", "tracepoints.txt")
    with open(path, "w") as fh:
        subprocess.check_call([sys.executable,
                               os.path.join(HERE, "tracepoints.py")], stdout=fh)
    return path


def prepare(runtime):
    """The scratch copy the runs happen in, with a flash of its own.

    The raw-data and vasistas commands write the external flash, and the dump
    the sim boots from carries the association secret, so this run gets a copy
    of it rather than the symlink abi/observe_calls.py's read-only runs take.
    """
    oc.prepare(runtime)
    os.makedirs(os.path.join(runtime, "out", "algos"), exist_ok=True)
    flash = os.path.join(runtime, "external_flash.bin")
    if os.path.islink(flash):
        os.remove(flash)
    if not os.path.exists(flash):
        shutil.copyfile(os.path.join(SIM, "external_flash.bin"), flash)


def write_scenario(scenario, runtime, hooks):
    counts = os.path.abspath(os.path.join(runtime, "out", "algos",
                                          "counts-%s.txt" % scenario.name))
    dump = os.path.join(runtime, "dump-%s.py" % scenario.name)
    with open(dump, "w") as fh:
        fh.write(DUMP % {"path": counts})
    path = os.path.join(runtime, "algos-%s.resc" % scenario.name)
    head = [":name: HWA10 dormant algorithms (%s)" % scenario.name,
            ":description: %s" % scenario.why,
            "", "# Generated by abi/algos.py, do not edit.", ""]
    if scenario.pipe:
        head += ["include @scripts/wpp-pipe.resc", "pause"]
    else:
        head += ["include @scripts/machine.resc",
                 "emulation SetAdvanceImmediately true"]
    if scenario.shell:
        # The console's reader only starts when the debug cable's RX line idles
        # high: 0x2e5e8 reads P0.08 and skips shell_init when it reads low.
        head += ["sysbus.gpio0 OnGPIO 8 true"]
    head += scenario.setup + [""]
    tail = ["start"] if (scenario.pipe or scenario.shell) else list(scenario.body)
    if not (scenario.pipe or scenario.shell):
        tail += ['python "exec(open(\'%s\').read())"' % os.path.abspath(dump),
                 "quit"]
    with open(path, "w") as fh:
        fh.write("\n".join(head) + "\n")
        fh.write(hooks)
        fh.write("\n" + "\n".join(tail) + "\n")
    return path, counts, dump


def emit(runtime):
    prepare(runtime)
    hooks = hook_block(tracepoints(runtime))
    return [write_scenario(s, runtime, hooks) for s in SCENARIOS.values()]


def scenario_port(scenario):
    return MONITOR_PORT + list(SCENARIOS).index(scenario.name)


def monitor(lines, port, pause=0.3):
    """Monitor commands over the telnet port, one line each.

    Renode serves that port only when it is not also serving the console: with
    `--console` the port is never bound, which is why the runs here are
    headless and read their output from the log file instead.
    """
    link = socket.create_connection(("127.0.0.1", port), timeout=10)
    link.settimeout(1)
    try:
        for line in lines:
            link.sendall((line + "\n").encode())
            time.sleep(pause)
            try:
                link.recv(65536)
            except socket.timeout:
                pass
    finally:
        link.close()


def type_line(text, port, uart, patience=60):
    """One console line, a character at a time, paced by the watch's own echo.

    shell_init's queue holds eight bytes (the xQueueCreate at 0x59f66), so a
    line handed to the UART in one burst is truncated at eight characters. What
    the reader can drain is not a wall-clock rate either -- a run under hooks
    advances virtual time a hundred times slower than one without -- so the
    pace is the echo the editor writes back at 0x59e12, which is the reader
    saying it took the character. The terminator is CR: the editor's jump table
    at 0x59dcc dispatches 0x0d and ignores 0x0a.
    """
    typed = ""
    for ch in text:
        monitor(["sysbus.uart0 WriteChar %d" % ord(ch)], port, pause=0.05)
        typed += ch
        deadline = time.time() + patience
        while time.time() < deadline:
            if open(uart, errors="replace").read().endswith(typed):
                break
            time.sleep(0.2)
        else:
            # A command that holds the console -- `max8617x test` prints "x :
            # return to shell" and keeps it until it is done -- drops what is
            # typed at it, and the run's counts are still the measurement, so
            # this reports the drop and the caller stops typing.
            return False
    monitor(["sysbus.uart0 WriteChar 13"], port, pause=0.05)
    return True


def shake_the_wrist(stop, port):
    while not stop.is_set():
        try:
            monitor(["spi2.adxl367 Motion 800 75"], port, pause=0.1)
        except OSError:
            return
        stop.wait(2.0)


def drive_shell(scenario, runtime, port):
    uart = os.path.join(runtime, "out", "uart0.log")
    if not oc.wait_for(uart, "shell>", 900):
        sys.exit("%s: the watch never printed a shell prompt" % scenario.name)
    transcript = open(os.path.join(runtime, "out", "algos",
                                   "shell-%s.txt" % scenario.name), "w")
    mark = len(open(uart, errors="replace").read())
    for line, dwell in scenario.shell:
        typed = type_line(line, port, uart)
        time.sleep(dwell)
        now = open(uart, errors="replace").read()
        transcript.write("\n===== %s =====\n" % line)
        transcript.write(now[mark:])
        transcript.flush()
        mark = len(now)
        if not typed:
            transcript.write("\n(the console stopped taking input here)\n")
            break
    transcript.close()


def run_scenario(scenario, runtime, counts, dump):
    import threading
    for stale in (counts, os.path.join(runtime, "out", "uart0.log")):
        if os.path.exists(stale):
            os.remove(stale)
    script = os.path.abspath(os.path.join(runtime,
                                          "algos-%s.resc" % scenario.name))
    log = open(os.path.join(runtime, "out", "algos", "%s.log" % scenario.name),
               "w")
    port = scenario_port(scenario)
    command = [oc.renode_binary(), "--disable-xwt", "--port", str(port),
               "-e", "include @%s" % script]
    stdin = subprocess.Popen(["sleep", "10800"], stdout=subprocess.PIPE)
    started = time.time()
    proc = subprocess.Popen(command, stdin=stdin.stdout, stdout=log, stderr=log,
                            cwd=runtime)
    stop = threading.Event()
    shaker = None
    try:
        if scenario.motion:
            shaker = threading.Thread(target=shake_the_wrist, args=(stop, port))
            shaker.daemon = True
            shaker.start()
        if scenario.pipe:
            oc.drive_pipe(scenario, runtime, log)
            time.sleep(scenario.settle)
        if scenario.shell:
            drive_shell(scenario, runtime, port)
        if scenario.pipe or scenario.shell:
            monitor(['python "exec(open(\'%s\').read())"'
                     % os.path.abspath(dump)], port, pause=5)
            proc.terminate()
        proc.wait(timeout=10800)
    finally:
        stop.set()
        if shaker is not None:
            shaker.join(timeout=5)
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=60)
        stdin.terminate()
        log.close()
    return time.time() - started


def read_counts(path):
    out = {}
    if not os.path.exists(path):
        return out
    for line in open(path):
        parts = line.split()
        if len(parts) != 4:
            continue
        out[int(parts[0], 16)] = dict(count=int(parts[1]), first=int(parts[2]),
                                      micros=float(parts[3]))
    return out


def report(runtime):
    smap = symmap.load()
    named = {s.address: s.name for s in smap.symbols}
    kinds = {}
    for line in open(os.path.join(runtime, "out", "tracepoints.txt")):
        address, why, name = line.split(None, 2)
        kinds[int(address, 16)] = why
    per = collections.OrderedDict(
        (name, read_counts(os.path.join(runtime, "out", "algos",
                                        "counts-%s.txt" % name)))
        for name in SCENARIOS)
    every = sorted(set().union(*[set(c) for c in per.values()]))
    print("%-10s %-8s %-30s %s"
          % ("address", "kind", "name", " ".join("%10s" % n[:10] for n in per)))
    for address in every:
        print("0x%08x %-8s %-30s %s"
              % (address, kinds.get(address, "-"),
                 named.get(address, "-")[:30],
                 " ".join("%10d" % per[n].get(address, {}).get("count", 0)
                          for n in per)))
    uncalled = [a for a in every if kinds.get(a) == "uncalled"]
    print("\n%d addresses ran, %d of them from the uncalled set"
          % (len(every), len(uncalled)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime", default=RUNTIME)
    ap.add_argument("--emit", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--scenario")
    args = ap.parse_args()
    runtime = os.path.abspath(args.runtime)
    written = emit(runtime) if (args.emit or args.run) else []
    if args.run:
        for (script, counts, dump), scenario in zip(written, SCENARIOS.values()):
            if args.scenario and scenario.name != args.scenario:
                continue
            took = run_scenario(scenario, runtime, counts, dump)
            print("%s: %d addresses in %.0f s"
                  % (scenario.name, len(read_counts(counts)), took))
    if args.report:
        report(runtime)


if __name__ == "__main__":
    main()
