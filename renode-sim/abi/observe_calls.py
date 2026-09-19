#!/usr/bin/env python3
"""What the running watch says, and which objects it says it about.

    python3 abi/observe_calls.py --runtime out/observe-calls/rt --emit
    python3 abi/observe_calls.py --runtime out/observe-calls/rt --run [--scenario NAME]
    python3 abi/observe_calls.py --runtime out/observe-calls/rt --report [--apply]

abi/autonames.py reads the firmware's own `[MODULE][%s]` logging out of the
image and names a function whenever the calling sequence carries the string.
Where the string is not in the calling sequence -- a `__func__` handed down as
a parameter, a tag out of a table, a format a wrapper was given -- the image
says where the value comes from and not what it is, which is what
abi/callargs.py's shapes record. A run says what it is: the logger is called
with the pointer in a register, so a hook at the logger's own entry reads the
string the same way the UART does.

So the observation is the five wlog entry points plus the FreeRTOS calls whose
arguments are handles, each recording the caller (`lr`, resolved to the
function that owns it), the instruction count and the raw argument registers.
A creation call also has its return address hooked, so the handle it produced
is known, and a handle later seen at a send or a take is that object under a
different name.

The line a site prints is only evidence about that site if it is the same line
every run, so a site is aggregated across every scenario and one that prints
two different things is reported rather than named. What is named goes in the
`runtime` class of abi/symbols.yaml with the scenario, the line and the site as
its evidence; the class is its own because it is the weakest reading of an
address in the map -- a run proves that a line was printed there and nothing
about the bytes.
"""

import argparse
import collections
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autonames  # noqa: E402
import kernel_objects  # noqa: E402
import symbols as symmap  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
ROOT = os.path.dirname(SIM)
CLASS = "runtime"
# One monitor port per scenario, so two of them can run at once on a host with
# the cores to spare; the workout is driven through it while the client has the
# pipe.
MONITOR_PORT = 12377

# The FreeRTOS calls whose arguments are the objects this is about: the creates
# say which object is which, and the rest say who uses it. A name the map does
# not carry is reported rather than skipped silently, because the hook set is
# part of the measurement.
KERNEL_APIS = sorted(kernel_objects.CREATES) + [
    "xQueueGenericSend", "xQueueSemaphoreTake", "xQueueReceive",
    "xTaskGenericNotify", "xTaskNotifyWait", "xTimerGenericCommand",
    "vTaskDelete",
]


# One hook per logger entry. The guard is the pair (entry, caller): the line a
# site prints is the thing being measured and a site in a loop would otherwise
# cost an IronPython call and a file write per iteration, which is what took
# abi/observe_words.py's display run from seconds to minutes. Reading the
# strings is left until the pair is new for the same reason.
WLOG_HOOK = """cpu AddHook 0x%(at)x \"\"\"
import sys
if not hasattr(sys, 'oc_seen'): sys.oc_seen = dict()
lr = cpu.GetRegisterUnsafe(14).RawValue & 0xfffffffe
key = (0x%(at)x, lr)
if key not in sys.oc_seen:
    sys.oc_seen[key] = 1
    out = ['w', '%(at)x', '%%x' %% lr, '%%d' %% cpu.ExecutedInstructions]
    bus = cpu.GetMachine().SystemBus
    for reg in (%(regs)s):
        p = cpu.GetRegisterUnsafe(reg).RawValue
        s = ''
        try:
            i = 0
            while i < 200:
                b = bus.ReadByte(p + i)
                if b == 0: break
                s += '%%02x' %% b
                i += 1
        except:
            s = ''
        out.append(s or '-')
    f = open('%(out)s', 'a'); f.write(' '.join(out) + '\\n'); f.close()
\"\"\"
"""

# A kernel call is recorded once per caller and handle: the handle is what the
# call is about, and one caller taking one mutex a thousand times says the same
# thing every time. Only the two registers the guard needs are read on the
# common path, because these entry points are the hottest code in the image --
# every mutex in the firmware goes through two of them -- and a hook costs an
# IronPython call whether it records anything or not.
#
# The hook takes itself out once it has either recorded %(keys)d distinct pairs
# or been through %(calls)d calls without finding a new one. Every one of these
# entry points is in the path of every mutex in the firmware, so leaving the
# hook in costs about two orders of magnitude of simulated speed for the rest
# of the run; what the removal gives up is a handle first used late, and what
# it buys is the run finishing at all.
KERNEL_HOOK = """cpu AddHook 0x%(at)x \"\"\"
import sys
if not hasattr(sys, 'oc_seen'): sys.oc_seen = dict()
n = sys.oc_seen.get(0x%(at)x, 0) + 1
sys.oc_seen[0x%(at)x] = n
lr = cpu.GetRegisterUnsafe(14).RawValue & 0xfffffffe
h = cpu.GetRegisterUnsafe(0).RawValue
key = (0x%(at)x, lr, h)
if key not in sys.oc_seen:
    sys.oc_seen[key] = 1
    k = sys.oc_seen.get(('n', 0x%(at)x), 0) + 1
    sys.oc_seen[('n', 0x%(at)x)] = k
    f = open('%(out)s', 'a')
    f.write('k %(at)x %%x %%d %%x %%x %%x %%x\\n' %% (lr, cpu.ExecutedInstructions, h, cpu.GetRegisterUnsafe(1).RawValue, cpu.GetRegisterUnsafe(2).RawValue, cpu.GetRegisterUnsafe(3).RawValue))
    f.close()
    if k >= %(keys)d: monitor.Parse('cpu RemoveHooksAt 0x%(at)x')
elif n >= %(calls)d:
    monitor.Parse('cpu RemoveHooksAt 0x%(at)x')
\"\"\"
"""

# How much of a run each kernel entry point is watched for.
KERNEL_KEYS, KERNEL_CALLS = 200, 60000

# The instruction after a create: r0 is the handle that create produced, which
# is what ties a handle seen at a send back to the call that named it.
RETURN_HOOK = """cpu AddHook 0x%(at)x \"\"\"
import sys
if not hasattr(sys, 'oc_seen'): sys.oc_seen = dict()
key = ('r', 0x%(at)x)
if key not in sys.oc_seen:
    sys.oc_seen[key] = 1
    f = open('%(out)s', 'a')
    f.write('r %(site)x %%d %%x\\n' %% (cpu.ExecutedInstructions, cpu.GetRegisterUnsafe(0).RawValue))
    f.close()
\"\"\"
"""


class Scenario(object):
    """One run: how the watch is set up, and what drives it from outside."""

    def __init__(self, name, why, setup=(), body=(), client=(), motion=False,
                 settle=5, hooks="wlog"):
        self.name = name
        self.why = why
        self.setup = list(setup)
        self.body = list(body)
        self.client = list(client)
        self.motion = motion
        self.settle = settle
        # Which hook family this run carries. The kernel entry points are the
        # hottest code in the image and a hook on one costs an IronPython call
        # per invocation, so a run that carries them advances a few hundred
        # thousand instructions a second instead of tens of millions. They go
        # in one run of their own, which is enough: a handle is created once
        # and used by the module that owns it in every scenario alike.
        self.hooks = hooks

    @property
    def pipe(self):
        return bool(self.client)


# The five abi/trace.py drives over the pipe, plus the two the rig drives by
# itself: the display run is every periodic thing the watch does with nobody
# talking to it, and the crown walk is the carousel.
SCENARIOS = collections.OrderedDict(
    (s.name, s) for s in [
        Scenario("display", "the watch face and everything the UI polls",
                 body=['emulation RunFor "60.0"']),
        Scenario("kernel", "the boot, with every FreeRTOS object it creates and"
                           " the first use of each",
                 body=['emulation RunFor "45.0"'], hooks="kernel"),
        Scenario("crown", "a click and a walk through the views",
                 body=['emulation RunFor "35.0"',
                       'sysbus.keys Tap "Enter"', 'emulation RunFor "2.0"',
                       'twi0.crown Rotate 1', 'emulation RunFor "2.0"',
                       'twi0.crown Rotate 1', 'emulation RunFor "2.0"',
                       'twi0.crown Rotate 1', 'emulation RunFor "2.0"',
                       'sysbus.keys Tap "Enter"', 'emulation RunFor "5.0"',
                       'twi0.crown Rotate -1', 'emulation RunFor "2.0"',
                       'twi0.crown Rotate -1', 'emulation RunFor "2.0"',
                       'sysbus.keys Tap "Enter"', 'emulation RunFor "20.0"']),
        Scenario("workout", "a workout with the wrist moving and a pulse",
                 setup=['spi1.max86173 Worn true',
                        'spi1.max86173 HeartRateBpm 128'],
                 client=["--workout", "120"], motion=True, settle=20),
        Scenario("hr", "an HR measurement, worn and still",
                 setup=['spi1.max86173 Worn true',
                        'spi1.max86173 HeartRateBpm 128'],
                 client=["--hr-measure", "90"]),
        Scenario("sleepmode", "the sleep window, worn and still at rest",
                 setup=['spi1.max86173 Worn true',
                        'spi1.max86173 HeartRateBpm 52'],
                 client=["--sleep", "120"]),
        Scenario("misc", "an alarm the phone sets and a notification",
                 client=["--alarm", "--notify"]),
        Scenario("ecg", "an ECG measurement",
                 setup=['spi1.max86173 Worn true',
                        'spi1.max86173 HeartRateBpm 70'],
                 client=["--ecg", "90"]),
    ])


class Targets(object):
    """Every address this hooks, and what each one is."""

    def __init__(self, image, dis, export):
        self.map = symmap.load()
        self.img = autonames.Image(image, dis)
        self.export = autonames.Export(export)
        if not self.export.ok:
            sys.exit("abi/observe_calls.py: no export under %s" % export)
        self.wlog = dict(autonames.WLOG_FMT_REG)
        self.apis, self.missing = {}, []
        for name in KERNEL_APIS:
            held = self.map.by_name.get(name)
            if held is None:
                self.missing.append(name)
                continue
            self.apis[held.address] = name
        creates = {self.map.by_name[a].address: a
                   for a in kernel_objects.CREATES if a in self.map.by_name}
        self.creates = kernel_objects.find_creates(self.img, self.export, creates)
        self.returns = {}
        index = {a: i for i, (a, _, _) in enumerate(self.img.insns)}
        for create in self.creates:
            after = self.img.insns[index[create.site] + 1][0]
            self.returns[after] = create.site

    def owner(self, address):
        return self.export.owner(address)


def observation_block(targets, out_path, family):
    """The hooks of one family, as the .resc lines that install them."""
    lines = []
    if family == "wlog":
        for at, reg in sorted(targets.wlog.items()):
            # The format is in `reg` and the varargs follow it; only the three
            # that fit in registers are read, which is the same cut
            # abi/autonames.py's static reading makes.
            regs = ", ".join(str(r) for r in range(reg, 4))
            lines.append(WLOG_HOOK % {"at": at, "regs": regs, "out": out_path})
        return "".join(lines)
    for at in sorted(targets.apis):
        lines.append(KERNEL_HOOK % {"at": at, "out": out_path,
                                    "keys": KERNEL_KEYS, "calls": KERNEL_CALLS})
    for at, site in sorted(targets.returns.items()):
        lines.append(RETURN_HOOK % {"at": at, "site": site, "out": out_path})
    return "".join(lines)


def prepare(runtime):
    """The scratch copy the runs happen in: Renode resolves @paths against it."""
    os.makedirs(os.path.join(runtime, "out"), exist_ok=True)
    # Copied, not linked: Renode resolves a script's `$ORIGIN` against the
    # script's own directory, so a linked scripts/ would send the watch log and
    # the frames back into renode-sim's out/ instead of this run's.
    for name in ("scripts", "models"):
        shutil.copytree(os.path.join(SIM, name), os.path.join(runtime, name),
                        dirs_exist_ok=True)
    for name in ("flash.bin", "appl.bin", "bl.bin", "sd.bin", "hwa10.repl",
                 "external_flash.bin", "NRF52840.svd"):
        source = os.path.join(SIM, name)
        link = os.path.join(runtime, name)
        if os.path.exists(source) and not os.path.exists(link):
            os.symlink(source, link)
    rig = os.path.join(runtime, "out", "rig")
    if not os.path.exists(rig):
        shutil.copytree(os.path.join(SIM, "out", "rig"), rig)
    os.makedirs(os.path.join(runtime, "out", "observe"), exist_ok=True)


def write_scenario(scenario, runtime, targets):
    # Absolute: the hooks run with Renode's working directory, which is the
    # runtime copy, and a path relative to it would name a different file
    # depending on where this generator was run from.
    out_path = os.path.abspath(os.path.join(runtime, "out", "observe",
                                            "obs-%s.txt" % scenario.name))
    path = os.path.join(runtime, "observe-%s.resc" % scenario.name)
    head = [":name: HWA10 call observation (%s)" % scenario.name,
            ":description: %s" % scenario.why,
            "",
            "# Generated by abi/observe_calls.py, do not edit.",
            ""]
    # The rig's own scripts are included whole so that a change to the machine
    # reaches these runs too. A pipe run's script ends in `start`, so the hooks
    # go on after a `pause` and the first instructions of the boot are the only
    # thing those two scenarios do not observe.
    if scenario.pipe:
        head += ["include @scripts/wpp-pipe.resc", "pause"]
    else:
        head += ["include @scripts/machine.resc",
                 "emulation SetAdvanceImmediately true"]
    head += scenario.setup + [""]
    body = ["start"] if scenario.pipe else list(scenario.body)
    with open(path, "w") as fh:
        fh.write("\n".join(head) + "\n")
        fh.write(observation_block(targets, out_path, scenario.hooks))
        fh.write("\n" + "\n".join(body) + "\n")
        if not scenario.pipe:
            fh.write("quit\n")
    return path, out_path


def emit(runtime, targets):
    prepare(runtime)
    written = []
    for scenario in SCENARIOS.values():
        written.append(write_scenario(scenario, runtime, targets))
    return written


def renode_binary():
    path = os.path.expanduser("~/renode-portable/renode")
    if not os.path.exists(path):
        sys.exit("%s is not there; the rig needs Renode 1.17" % path)
    return path


def monitor_lines(lines, port=MONITOR_PORT):
    """Send monitor commands to a running Renode, one line each."""
    try:
        link = socket.create_connection(("127.0.0.1", port), timeout=5)
    except OSError:
        return False
    try:
        for line in lines:
            link.sendall((line + "\n").encode())
            time.sleep(0.2)
    finally:
        link.close()
    return True


def scenario_port(scenario):
    return MONITOR_PORT + list(SCENARIOS).index(scenario.name)


def shake_the_wrist(stop, port):
    """The motion a workout is: the accelerometer given a turn every few seconds.

    The wrist is what separates the workout scenario from the still ones, and
    the model takes it as a command, so it is driven from here rather than from
    the script: the script has already handed the run to the client.
    """
    while not stop.is_set():
        monitor_lines(["spi2.adxl367 Motion 800 75"], port)
        stop.wait(2.0)


def wait_for(path, needle, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path) and needle in open(path, errors="replace").read():
            return True
        time.sleep(1)
    return False


def drive_pipe(scenario, runtime, log):
    uart = os.path.join(runtime, "out", "uart0.log")
    if not wait_for(uart, "Add WPPS chars.", 900):
        sys.exit("%s: the watch never advertised the WPP service" % scenario.name)
    args = [os.path.join(ROOT, "target", "debug", "wpp-sim-client"),
            "--secret-from-dump",
            os.path.abspath(os.path.join(runtime, "external_flash.bin")),
            "--set-time", str(int(time.time()))] + scenario.client
    stop = threading.Event()
    shaker = None
    if scenario.motion:
        shaker = threading.Thread(target=shake_the_wrist,
                                  args=(stop, scenario_port(scenario)))
        shaker.daemon = True
        shaker.start()
    try:
        if subprocess.call(args, stdout=log, stderr=log, cwd=runtime):
            sys.exit("%s: wpp-sim-client failed; see out/observe/%s.log"
                     % (scenario.name, scenario.name))
    finally:
        stop.set()
        if shaker is not None:
            shaker.join(timeout=5)


def run_scenario(scenario, runtime):
    out_path = os.path.join(runtime, "out", "observe",
                            "obs-%s.txt" % scenario.name)
    for stale in (out_path, os.path.join(runtime, "out", "uart0.log")):
        if os.path.exists(stale):
            os.remove(stale)
    script = os.path.abspath(os.path.join(runtime,
                                          "observe-%s.resc" % scenario.name))
    log = open(os.path.join(runtime, "out", "observe", "%s.log" % scenario.name), "w")
    command = [renode_binary(), "--console", "--disable-xwt",
               "--port", str(scenario_port(scenario)),
               "-e", "include @%s" % script]
    # Renode reads stdin even headless and spins on a closed one, so it is
    # given one that stays open for as long as the run can take.
    stdin = subprocess.Popen(["sleep", "7200"], stdout=subprocess.PIPE)
    started = time.time()
    proc = subprocess.Popen(command, stdin=stdin.stdout, stdout=log, stderr=log,
                            cwd=runtime)
    try:
        if scenario.pipe:
            drive_pipe(scenario, runtime, log)
            # Every hook writes and closes its line as it fires, so the
            # observation is on disk; the wait is for what the watch does after
            # the phone has gone away.
            time.sleep(scenario.settle)
            proc.terminate()
        proc.wait(timeout=3600)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=60)
        stdin.terminate()
        log.close()
    return time.time() - started


# ------------------------------------------------------------------ reading

class Line(object):
    """One logger call as a run saw it: who called, and what was printed."""

    def __init__(self, entry, caller, count, fmt, args):
        self.entry = entry
        self.caller = caller
        self.count = count
        self.fmt = fmt
        self.args = args

    def text(self):
        """The format with its `%s` conversions replaced by what they printed.

        Only the `%s` ones: the numeric conversions' values are in the
        registers as numbers and the widths and flags are the logger's
        business, so a format keeps them and what it prints for them is not
        this measurement's claim.
        """
        out, arg = [], 0
        at = 0
        for m in autonames.SPEC.finditer(self.fmt):
            out.append(self.fmt[at:m.start()])
            at = m.end()
            if m.group(1) == "%":
                out.append("%%")
                continue
            if m.group(1) == "s" and arg < len(self.args) \
                    and self.args[arg] is not None:
                out.append(self.args[arg])
            else:
                out.append(m.group(0))
            arg += 1
        out.append(self.fmt[at:])
        return "".join(out).rstrip("\n")


def unhex(text):
    if text == "-":
        return None
    try:
        return bytes.fromhex(text).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def parse_observation(path, targets):
    """(lines, kernel calls, handles) out of one scenario's hook output."""
    lines, calls, handles = [], [], {}
    if not os.path.exists(path):
        return lines, calls, handles
    for raw in open(path, errors="replace"):
        parts = raw.split()
        if not parts:
            continue
        if parts[0] == "w" and len(parts) >= 5:
            entry, caller, count = (int(parts[1], 16), int(parts[2], 16),
                                    int(parts[3]))
            fields = [unhex(p) for p in parts[4:]]
            if fields[0] is None:
                continue
            lines.append(Line(entry, caller, count, fields[0], fields[1:]))
        elif parts[0] == "k" and len(parts) == 8:
            calls.append({"entry": int(parts[1], 16), "caller": int(parts[2], 16),
                          "count": int(parts[3]),
                          "args": [int(p, 16) for p in parts[4:]]})
        elif parts[0] == "r" and len(parts) == 4:
            handles.setdefault(int(parts[3], 16), int(parts[1], 16))
    return lines, calls, handles


def per_site(observations):
    """(entry, caller) -> {scenario: the text that site printed}."""
    out = collections.defaultdict(dict)
    for name, (lines, _, _) in sorted(observations.items()):
        for line in lines:
            out[(line.entry, line.caller)].setdefault(name, line.text())
    return out


def settled(sites):
    """The sites that printed one thing, and the ones that printed several."""
    agreed, disagreed = {}, {}
    for key, texts in sites.items():
        distinct = sorted(set(texts.values()))
        if len(distinct) == 1:
            agreed[key] = (distinct[0], sorted(texts))
        else:
            disagreed[key] = texts
    return agreed, disagreed


def names_from_lines(targets, agreed):
    """Apply abi/autonames.py's naming rules to what the runs printed.

    The rules are the image's own conventions and they do not change because
    the string arrived at run time rather than in a literal pool: a bare
    snake_case identifier in a `%s` is `__func__`, a `[tag]` of function shape
    is `__func__` with the format already expanded, and a leading identifier
    that ends in `_cb` or `_handler` is a name no variable carries. A name that
    is one function's and no other function's is the only one taken.
    """
    candidates, where = collections.defaultdict(set), {}
    for (entry, caller), (text, runs) in sorted(agreed.items()):
        fn = targets.owner(caller)
        if fn is None:
            continue
        for ident in identifiers(entry, text):
            candidates[fn].add(ident)
            where.setdefault((fn, ident), (text, runs, caller))
    by_ident = collections.defaultdict(set)
    for fn, idents in candidates.items():
        for ident in idents:
            by_ident[ident].add(fn)
    rows = []
    for fn, idents in sorted(candidates.items()):
        if len(idents) != 1:
            continue
        name = next(iter(idents))
        if len(by_ident[name]) != 1:
            continue
        text, runs, caller = where[(fn, name)]
        rows.append({"address": fn, "name": name, "kind": "function",
                     "class": CLASS,
                     "evidence": "observed at runtime (%s): wlog(%s) from 0x%x"
                                 % (",".join(runs), text, caller)})
    return rows


def identifiers(entry, text):
    """Every name the naming rules read out of one printed line."""
    found = set()
    tag = autonames.LOG_TAG.match(text)
    if tag and autonames.FUNC_IDENT.match(tag.group(1).encode()):
        found.add(tag.group(1))
    lead = autonames.LEADING_IDENT.match(text)
    if lead and lead.group(1).endswith(autonames.CALLBACK_SUFFIX) \
            and autonames.FUNC_IDENT.match(lead.group(1).encode()):
        found.add(lead.group(1))
    # The `__func__` case: the expansion put the identifier in the line, so it
    # is a bare snake_case word inside the tag brackets or right after them.
    inner = re.match(r"^\[[^\]]{1,24}\]\[([^\]]{1,40})\]", text)
    if inner and autonames.FUNC_IDENT.match(inner.group(1).encode()):
        found.add(inner.group(1))
    # A line that is nothing but an identifier is `wlog("%s\n", __func__)` with
    # the expansion already done: no module prefix, no message, and a bare
    # snake_case word is not a sentence anyone writes.
    bare = text.strip().rstrip(":")
    if autonames.FUNC_IDENT.match(bare.encode()):
        found.add(bare)
    return found


def object_rows(targets, observations, held):
    """Names for the objects a create made that the image alone did not name.

    A create the static reading could not attribute produced a handle, the runs
    say which handle, and the sends and takes of that handle say which module
    uses it. That is the same rule abi/kernel_objects.py applies to the
    handle's static readers, with the run supplying the readers the image does
    not show.
    """
    modules = kernel_objects.Modules(os.path.join(HERE, "out", "ghidra"))
    by_site = {c.site: c for c in targets.creates}
    users = collections.defaultdict(collections.Counter)
    # A static create returns the object it was handed, which the runs confirm
    # address for address, so the static object is the handle whether or not a
    # run caught the return.
    handles = {}
    for create in targets.creates:
        at = create.value(1 if create.api == "xQueueCreateMutexStatic" else 3,
                          kernel_objects.RAM_LO, kernel_objects.RAM_HI)
        if at is not None and not create.api.startswith("xTask"):
            handles[at] = create.site
    for _, (_, calls, made) in sorted(observations.items()):
        handles.update(made)
        for call in calls:
            owner = targets.owner(call["caller"])
            tag = modules.named(owner) if owner is not None else None
            if tag:
                users[call["args"][0]][tag] += 1
    rows = []
    for handle, site in sorted(handles.items()):
        create = by_site.get(site)
        if create is None or create.api.startswith("xTask"):
            continue
        top = users[handle].most_common()
        if not top or (len(top) > 1 and top[0][1] == top[1][1]):
            continue
        kind = kernel_objects.QUEUE_TYPE.get(create.queue_type, "queue")
        base = top[0][0]
        name = base if base.endswith("_" + kind) else "%s_%s" % (base, kind)
        statics = [(create.value(1, kernel_objects.RAM_LO, kernel_objects.RAM_HI),
                    "static")] if create.api == "xQueueCreateMutexStatic" else \
            [(create.value(3, kernel_objects.RAM_LO, kernel_objects.RAM_HI), "static"),
             (create.value(2, kernel_objects.RAM_LO, kernel_objects.RAM_HI), "storage")]
        for at, suffix in [(create.handle, None)] + statics:
            if at is None or at in held:
                continue
            rows.append({"address": at,
                         "name": name if suffix is None else "%s_%s" % (name, suffix),
                         "kind": "global", "class": CLASS, "module": base,
                         "evidence": "the handle 0x%x that %s at 0x%x produced is"
                                     " taken and given only in %s"
                                     % (handle, create.api, create.site, base)})
    return rows


def report(targets, runtime, apply_names):
    observations = {}
    for name in SCENARIOS:
        path = os.path.join(runtime, "out", "observe", "obs-%s.txt" % name)
        if os.path.exists(path):
            observations[name] = parse_observation(path, targets)
    if not observations:
        sys.exit("no observation files in %s; run --run first" % runtime)
    sites = per_site(observations)
    agreed, disagreed = settled(sites)
    prior = symmap.load()
    # An entry a previous run of this tool wrote is this run's own output and
    # not the image's naming: counting it as held would make the second run
    # propose nothing and the rewrite then take the first run's answers out.
    held = {a for a, s in prior.by_address.items() if s.klass != CLASS}
    unnamed = {fn for fn in targets.export.fns
               if targets.export.unnamed(fn) and fn not in held}
    rows = [r for r in names_from_lines(targets, agreed)
            if r["address"] in unnamed]
    rows += object_rows(targets, observations, held)
    rows = [r for r in rows if r["address"] not in held]

    print("scenarios: %s" % ", ".join(sorted(observations)))
    for name, (lines, calls, made) in sorted(observations.items()):
        print("  %-10s %5d logger sites, %4d kernel calls, %2d handles"
              % (name, len(lines), len(calls), len(made)))
    print("%d logger call sites over all scenarios, %d printed one line,"
          " %d printed several" % (len(sites), len(agreed), len(disagreed)))
    for key, texts in sorted(disagreed.items())[:20]:
        print("  0x%x from 0x%x: %s" % (key[0], key[1],
                                        " | ".join(sorted(set(texts.values())))))
    print("%d names: %d functions, %d objects"
          % (len(rows), sum(1 for r in rows if r["kind"] == "function"),
             sum(1 for r in rows if r["kind"] == "global")))
    for row in sorted(rows, key=lambda r: r["address"]):
        print("  0x%x %-40s %s" % (row["address"], row["name"], row["evidence"]))
    verdicts = os.path.join(runtime, "out", "observe", "verdicts.json")
    with open(verdicts, "w") as fh:
        json.dump({"scenarios": sorted(observations),
                   "sites": [{"entry": "0x%x" % e, "caller": "0x%x" % c,
                              "function": ("0x%x" % targets.owner(c)
                                           if targets.owner(c) else None),
                              "text": t, "runs": runs}
                             for (e, c), (t, runs) in sorted(agreed.items())],
                   "disagreed": [{"entry": "0x%x" % e, "caller": "0x%x" % c,
                                  "texts": t}
                                 for (e, c), t in sorted(disagreed.items())],
                   "names": rows}, fh, indent=1)
    print("%s" % os.path.relpath(verdicts, runtime))
    if not apply_names:
        return 0
    try:
        added = symmap.load().rewrite(rows, {CLASS})
    except symmap.Refusal as err:
        sys.exit("abi/observe_calls.py: abi/symbols.yaml: %s" % err)
    print("wrote %d entries to abi/symbols.yaml" % added)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime", required=True,
                    help="the scratch copy of renode-sim the runs happen in")
    ap.add_argument("--image", default=os.path.join(SIM, "appl.bin"))
    ap.add_argument("--dis", default=os.path.join(SIM, "out", "appl.dis"))
    ap.add_argument("--export", default=os.path.join(HERE, "out", "ghidra"))
    ap.add_argument("--emit", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--apply", action="store_true",
                    help="write the observed names into abi/symbols.yaml")
    ap.add_argument("--scenario", action="append", default=[],
                    choices=sorted(SCENARIOS))
    args = ap.parse_args()

    targets = Targets(args.image, args.dis, args.export)
    for name in targets.missing:
        print("abi/observe_calls.py: abi/symbols.yaml does not name %s, so its"
              " calls are not observed" % name)
    if args.emit or args.run:
        written = emit(args.runtime, targets)
        print("%d logger entries, %d kernel entries, %d create returns hooked"
              % (len(targets.wlog), len(targets.apis), len(targets.returns)))
        for path, _ in written:
            print("  %s" % os.path.relpath(path, args.runtime))
    if args.run:
        for name in (args.scenario or list(SCENARIOS)):
            took = run_scenario(SCENARIOS[name], args.runtime)
            lines, calls, made = parse_observation(
                os.path.join(args.runtime, "out", "observe", "obs-%s.txt" % name),
                targets)
            print("  %-10s %6.1f s wall, %d logger sites, %d kernel calls,"
                  " %d handles" % (name, took, len(lines), len(calls), len(made)))
    if args.report:
        return report(targets, args.runtime, args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
