#!/usr/bin/env python3
"""What the running watch does with the words the classification could not decide.

    python3 abi/observe_words.py --runtime <scratch dir> --emit
    python3 abi/observe_words.py --runtime <scratch dir> --run [--scenario NAME]
    python3 abi/observe_words.py --runtime <scratch dir> --report [--apply]

The shape signals in abi/classify_words.py leave about 800 words undecided:
`lands_in_untyped_run`, where the value is inside one of the record tables the
analysis has no layout for, and `not_a_word_slot`, where the word is a 4-aligned
window over an object whose stride is not four. Nothing about the bytes settles
those, and DEVELOPMENT.md's reading of what tracing is good for is the way in:
execution proves liveness and never deadness, but it settles individual words,
because a value the code dereferences is an address and nothing else.

So the observation is not "which instructions ran" but "was this word's value
ever used as an address", and the pair of facts that answers it is a hook on
every instruction that loads the word and a watchpoint on the address the word
holds. A pool word is loaded by the `ldr rN,[pc,#k]` the export attributes to
it; a word inside a run is reached by loading the run's base and indexing, so
its load sites are the pc-relative loads of every pool word whose value lands in
the run. The watchpoint fires on the first read of the target, and a hook at the
target catches the case where the value is branched to instead. Both record the
instruction count, so a target touched before the word was ever loaded is not
counted as that word's dereference.

Three buckets come out of that, per word and over all the scenarios:

  observed_address     some load site ran and the target was touched after it,
                       which makes the word a pointer.
  observed_value_only  a load site ran on every scenario that reached it and the
                       target was never touched. This one is measured and not
                       applied: declaring those 158 words constant and linking
                       `GC=1 PRUNE=wpps_tls_tunnel DATA=1` boots into an
                       undefined instruction, because a word no run
                       dereferenced was still the only thing holding a body
                       alive. The runs falsify a pointer; they cannot establish
                       a number.
  never_loaded         no load site ran at all, so the runs say nothing and the
                       word stays on the review list.

The scenarios are the paths the rig can drive: the 60 s display run, the WPP
walk with a time write, an HR measurement, the crown, and the update. `--run`
runs them all in the scratch copy `--runtime` names and leaves one observation
file per scenario in its out/observe/; `--report` aggregates those, and
`--apply` writes the address bucket into abi/words.yaml as overrides whose
reason carries the run and the site that decided each one; the other two are
recorded in out/observe/verdicts.json and change nothing.
"""

import argparse
import bisect
import collections
import json
import os
import subprocess
import sys
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
ROOT = os.path.dirname(SIM)

APP_BASE, APP_END = 0x27000, 0xF117C

MARKER = "# --- observed at runtime by abi/observe_words.py; regenerate, do not edit ---"


class Analysis(object):
    """The export and the classification, indexed the way the sites need them."""

    def __init__(self, export, image, facts):
        with open(os.path.join(export, "items.json")) as fh:
            self.items = json.load(fh)
        with open(os.path.join(export, "references.json")) as fh:
            self.refs = json.load(fh)
        with open(os.path.join(export, "words.json")) as fh:
            self.words = json.load(fh)["words"]
        with open(image, "rb") as fh:
            self.blob = fh.read()
        bits = bytes.fromhex(self.items["instruction_bytes"]["bits"])
        self.covered = bytes((bits[i >> 3] >> (i & 7)) & 1
                             for i in range(len(self.blob)))
        spans = [(d["start"], d["end"]) for d in self.items["data"]]
        spans += [(g["start"], g["end"]) for g in self.items["gaps"]]
        self.runs = sorted(spans)
        self.run_starts = [s for s, _ in self.runs]
        self.by_target = collections.defaultdict(list)
        self.by_value = collections.defaultdict(list)
        for r in self.refs["pool_reads"]:
            self.by_target[r["target"]].append(r["site"])
            self.by_value[self.held(r["target"])].append(r["site"])
        self.values = sorted(self.by_value)
        # An entry this tool wrote hides the signal that put the word on the
        # review list, so the entry carries it and it is read back here.
        self.signals = {}
        for word in self.words:
            self.signals[word["addr"]] = word["signal"]
        for entry in yaml.safe_load(open(facts))["overrides"]:
            observed = entry.get("observed")
            if observed:
                self.signals[entry["address"]] = observed["signal"]

    def shape_signal(self, addr):
        """The signal the shape rules gave this word before any run decided it."""
        return self.signals[addr]

    def held(self, addr):
        at = addr - APP_BASE
        return int.from_bytes(self.blob[at:at + 4], "little")

    def run_of(self, addr):
        i = bisect.bisect_right(self.run_starts, addr) - 1
        if i >= 0 and self.runs[i][1] > addr:
            return self.runs[i]
        return None

    def load_sites(self, word):
        """Every instruction whose execution puts this word's value in a register.

        A pool word has its own pc-relative load. A word in a run has none: the
        code loads the run's base and indexes, so the sites are the loads of
        every pool word that names an address inside the same run. That is wider
        than the word -- a load of the base is a load of the whole table -- and
        it has to be, because the index is computed and the export does not
        carry it; the effect is that the run's words share a verdict, which is
        what a table's column is anyway.
        """
        if word["kind"] in ("pool", "jumptable"):
            return sorted(set(self.by_target.get(word["addr"], ())))
        span = self.run_of(word["addr"])
        if span is None:
            return []
        lo, hi = span
        sites = set()
        i = bisect.bisect_left(self.values, lo)
        while i < len(self.values) and self.values[i] < hi:
            sites.update(self.by_value[self.values[i]])
            i += 1
        return sorted(sites)


def is_subject(word):
    """Is this a word the runs are being asked about?

    The undecided ones, and the ones a previous run of this tool decided: an
    entry it wrote is a measurement it owns, so re-running has to measure the
    same set and not the set minus its own answers.
    """
    if word["class"] == "review":
        return True
    return word["signal"] == "override" and word["note"].startswith("observed_")


def review_sites(analysis):
    """(word row, load sites, watch target, executable target) per review word."""
    out = []
    for word in analysis.words:
        if not is_subject(word):
            continue
        value = word["value"]
        target = value & ~1
        inside = APP_BASE <= target < APP_END
        executable = inside and bool(analysis.covered[target - APP_BASE])
        out.append((word, analysis.load_sites(word),
                    target if inside else None,
                    target if executable else None))
    return out


# The hook removes itself once it has written its line. Recording the first
# execution is the whole question, and a site inside a loop otherwise costs an
# IronPython call per iteration, which took the 60 s display run from 16 s of
# wall time to more than ten minutes.
HOOK = """cpu AddHook 0x%(at)x \"\"\"
g=globals()
if 'k%(at)x' not in g:
    g['k%(at)x']=1
    f=open('%(out)s','a'); f.write('%(kind)s 0x%(at)x %%d\\n' %% cpu.ExecutedInstructions); f.close()
    monitor.Parse('cpu RemoveHooksAt 0x%(at)x')
\"\"\"
"""

# A watchpoint cannot be removed one at a time, so this one keeps the guard; it
# is a dictionary lookup on a read of four bytes of the image and the 60 s run
# pays about eight seconds for the whole set.
WATCH = """sysbus AddWatchpointHook 0x%(at)x Byte Read \"\"\"
g=globals()
if 'w%(at)x' not in g:
    g['w%(at)x']=1
    f=open('%(out)s','a'); f.write('read 0x%(at)x %%d\\n' %% cpu.ExecutedInstructions); f.close()
\"\"\"
"""


def observation_block(sites, out_path):
    """The hooks, one per distinct address, whatever number of words wants it.

    The file the hooks write is the set of addresses the run touched and the
    instruction count of the first touch; nothing in it grows with how often a
    site runs, because a site hook takes itself out once it has written.
    """
    lines, seen = [], set()
    for _, loads, watch, execute in sites:
        for site in loads:
            if ("s", site) not in seen:
                seen.add(("s", site))
                lines.append(HOOK % {"at": site, "out": out_path,
                                     "kind": "site"})
        if watch is not None and ("w", watch) not in seen:
            seen.add(("w", watch))
            lines.append(WATCH % {"at": watch, "out": out_path})
        if execute is not None and ("x", execute) not in seen:
            seen.add(("x", execute))
            lines.append(HOOK % {"at": execute, "out": out_path,
                                 "kind": "exec"})
    return "".join(lines)


# Each scenario is the monitor lines that drive it after the machine and the
# hooks are in place, and whether it needs the WPP pipe and a client run. The
# inputs are the ones FIRMWARE_TOOLING.md gives for each path; the waits are
# virtual seconds, and the display's 30 s start-up delay is why the crown and
# the sensor scenarios wait before they poke anything.
SCENARIOS = {
    "display": {
        "why": "the watch face, the carousel timer and everything the UI polls",
        "pipe": False,
        "body": ['emulation SetAdvanceImmediately true',
                 'emulation RunFor "60.0"'],
    },
    "hr": {
        "why": "an HR measurement: worn, a rate, and the measurement burst",
        "pipe": False,
        "body": ['emulation SetAdvanceImmediately true',
                 'emulation RunFor "35.0"',
                 'spi1.max86173 Worn true',
                 'spi1.max86173 HeartRateBpm 70',
                 'emulation RunFor "90.0"'],
    },
    "crown": {
        "why": "a click and rotations through the carousel",
        "pipe": False,
        "body": ['emulation SetAdvanceImmediately true',
                 'emulation RunFor "35.0"',
                 'sysbus.keys Tap "Enter"',
                 'emulation RunFor "2.0"',
                 'twi0.crown Rotate 1',
                 'emulation RunFor "1.0"',
                 'twi0.crown Rotate 1',
                 'emulation RunFor "1.0"',
                 'twi0.crown Rotate -1',
                 'emulation RunFor "1.0"',
                 'sysbus.keys Tap "Enter"',
                 'emulation RunFor "20.0"'],
    },
    "wpp": {
        "why": "the phone: the challenge, a time write and the vasistas walk",
        "pipe": True,
        "client": ["--set-time"],
    },
    "update": {
        "why": "the update path, which is the only run that enters the bootloader",
        "pipe": True,
        "update": True,
        "settle": 120,
        "client": ["--update"],
    },
}


def write_scenario(name, spec, runtime, sites):
    """The .resc for one scenario: the rig's own setup, the hooks, the inputs."""
    out_dir = os.path.join(runtime, "out", "observe")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "obs-%s.txt" % name)
    path = os.path.join(runtime, "observe-%s.resc" % name)
    head = [":name: HWA10 word observation (%s)" % name,
            ":description: %s" % spec["why"],
            "",
            "# Generated by abi/observe_words.py, do not edit.",
            ""]
    # The rig's own scripts are included whole rather than copied into this
    # generator, so a change to the machine or to the update chain reaches these
    # runs too. The two that drive the watch from outside end in `start`, so the
    # hooks go on after a `pause`: what executes between those two lines is
    # unobserved, and it is the first instructions of a boot the other three
    # scenarios observe from reset.
    if spec.get("update"):
        head += ["include @scripts/update-test.resc", "pause"]
    elif spec["pipe"]:
        head += ["include @scripts/wpp-pipe.resc", "pause"]
    else:
        head.append("include @scripts/machine.resc")
    head.append("")
    # A pipe run is driven from outside: the client connects, does its work and
    # the driver sends `quit` over the monitor port, so the script neither runs
    # for a fixed time nor ends itself.
    body = ["start"] if spec["pipe"] else list(spec.get("body", []))
    with open(path, "w") as fh:
        fh.write("\n".join(head) + "\n")
        fh.write(observation_block(sites, out_path))
        fh.write("\n" + "\n".join(body) + "\n")
        if not spec["pipe"]:
            fh.write("quit\n")
    return path, out_path


def emit(runtime, sites, analysis):
    written = []
    for name, spec in sorted(SCENARIOS.items()):
        path, out_path = write_scenario(name, spec, runtime, sites)
        written.append((name, path, out_path))
    index = {"scenarios": {n: os.path.relpath(o, runtime)
                           for n, _, o in written},
             "words": [{"addr": w["addr"], "value": w["value"],
                        "signal": analysis.shape_signal(w["addr"]),
                        "sites": loads,
                        "watch": watch, "exec": execute}
                       for w, loads, watch, execute in sites]}
    with open(os.path.join(runtime, "out", "observe", "sites.json"), "w") as fh:
        json.dump(index, fh, indent=1)
    return written


def renode_binary():
    path = os.path.expanduser("~/renode-portable/renode")
    if not os.path.exists(path):
        sys.exit("%s is not there; the rig needs Renode 1.17" % path)
    return path


def run_scenario(name, spec, runtime):
    """Start Renode on the scenario and drive whatever it needs from outside."""
    out_path = os.path.join(runtime, "out", "observe", "obs-%s.txt" % name)
    if os.path.exists(out_path):
        os.remove(out_path)
    script = os.path.join(runtime, "observe-%s.resc" % name)
    # The driver waits for the WPP service on the watch log, so last run's copy
    # of it has to go or the client connects before the pipe is listening.
    uart = os.path.join(runtime, "out", "uart0.log")
    if os.path.exists(uart):
        os.remove(uart)
    log = open(os.path.join(runtime, "out", "observe", "%s.log" % name), "w")
    # No $ORIGIN: an include rebinds it to the included script's own directory,
    # so the rig's outputs are named against the working directory instead.
    command = [renode_binary(), "--console", "--disable-xwt",
               "-e", "include @%s" % script]
    if spec.get("update"):
        # scripts/update-test.resc reads the rig of the image the bootloader installs,
        # which for this run is the image it booted.
        subprocess.check_call(
            [sys.executable, os.path.join(HERE, "rig.py"),
             "--image", os.path.join(runtime, "flash.bin"),
             "--symbols", "partition",
             "--out", os.path.join(runtime, "out", "rig-updated")],
            stdout=subprocess.DEVNULL, cwd=runtime)
    started = time.time()
    # Renode reads stdin even headless and spins on a closed one, which is why
    # the rig always gives it one that stays open.
    stdin = subprocess.Popen(["sleep", "3600"], stdout=subprocess.PIPE)
    proc = subprocess.Popen(command, stdin=stdin.stdout, stdout=log, stderr=log,
                            cwd=runtime)
    try:
        if spec["pipe"]:
            drive_pipe(name, spec, runtime, log)
            # A pipe script never ends itself, and there is nothing to ask it
            # for: every hook writes and closes its line as it fires, so the
            # observation is on disk and the run is over when the client is
            # done with it. The wait is for what the watch does afterwards --
            # the update's reboot into the installed image is the reason.
            time.sleep(spec.get("settle", 5))
            proc.terminate()
        proc.wait(timeout=1800)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=60)
        stdin.terminate()
        log.close()
    return time.time() - started


def wait_for(path, needle, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path) and needle in open(path, errors="replace").read():
            return True
        time.sleep(1)
    return False


def drive_pipe(name, spec, runtime, log):
    """The client half of a pipe run: wait for the link, then send commands."""
    uart = os.path.join(runtime, "out", "uart0.log")
    if not wait_for(uart, "Add WPPS chars.", 600):
        sys.exit("%s: the watch never advertised the WPP service" % name)
    args = [os.path.join(ROOT, "target", "debug", "wpp-sim-client"),
            "--secret-from-dump", os.path.join(runtime, "external_flash.bin")]
    for flag in spec["client"]:
        if flag == "--set-time":
            args += ["--set-time", str(int(time.time()))]
        elif flag == "--update":
            package = os.path.join(runtime, "out", "observe", "update.bin")
            subprocess.check_call(
                [sys.executable, os.path.join(ROOT, "tools", "mkpkg.py"),
                 "--version", "9999",
                 os.path.join(ROOT, "hwa10_3411_Tf4fD4.bin"), package])
            args += ["--update", package]
    if subprocess.call(args, stdout=log, stderr=log, cwd=runtime):
        sys.exit("%s: wpp-sim-client failed; see out/observe/%s.log"
                 % (name, name))


def parse_observation(path):
    """(kind, address) -> the instruction count of the first time it happened."""
    seen = {}
    if not os.path.exists(path):
        return seen
    for line in open(path):
        parts = line.split()
        if len(parts) != 3:
            continue
        key = (parts[0], int(parts[1], 16))
        count = int(parts[2])
        if key not in seen or count < seen[key]:
            seen[key] = count
    return seen


def verdicts(sites, observations):
    """Per word: which bucket, and the run and address that put it there."""
    out = {}
    for word, loads, watch, execute in sites:
        loaded, touched = [], []
        for name, seen in sorted(observations.items()):
            first = min([seen[("site", s)] for s in loads
                         if ("site", s) in seen] or [None])
            if first is None:
                continue
            loaded.append(name)
            for kind, addr in (("read", watch), ("exec", execute)):
                if addr is None or (kind, addr) not in seen:
                    continue
                if seen[(kind, addr)] >= first:
                    touched.append((name, kind, addr))
        if touched:
            name, kind, addr = touched[0]
            out[word["addr"]] = ("observed_address", name,
                                 "0x%x %s after a load" % (addr, kind))
        elif loaded:
            out[word["addr"]] = ("observed_value_only", ",".join(loaded),
                                 "loaded, target never touched")
        else:
            out[word["addr"]] = ("never_loaded", "", "")
    return out


# Only the address half is written into abi/words.yaml. The other half was
# falsified the first time it was applied: with the 157 observed_value_only
# words declared constant, `GC=1 PRUNE=wpps_tls_tunnel DATA=1` boots into an
# undefined instruction at 0x98a2e, because a word no run dereferenced was the
# only thing holding that body alive and --gc-sections took it. "The paths the
# rig drives never used it as an address" is not "it is not an address", which
# is the same lesson abi/ghidra/word_uses.py's constant half already taught.
APPLIED = ("observed_address",)

REASONS = {
    "observed_address": """
    The runs dereferenced it. A hook on every instruction that loads this word
    recorded the instruction count of the first load, and a watchpoint on the
    address it holds recorded the first read of those bytes; the read came after
    the load, so the value reached memory as an address. A word in a run shares
    its load sites with the run's base, so what this proves for a table column
    is that the column is read through, which is what makes it a pointer field.
""",
}


def apply_to_words_yaml(path, decided, analysis):
    """Rewrite the generated block of abi/words.yaml from the observations."""
    text = open(path).read()
    head = text.split(MARKER)[0].rstrip("\n")
    rows = []
    for addr in sorted(decided):
        reason, run, note = decided[addr]
        if reason not in APPLIED:
            continue
        value = analysis.held(addr)
        klass = "pointer" if reason == "observed_address" else "constant"
        row = ["  - address: 0x%x" % addr,
               "    value: 0x%08x" % value,
               "    class: %s" % klass]
        if klass == "pointer":
            # The value is the address, bit 0 and all: an odd one naming code is
            # a Thumb function pointer and the relocation has to keep the bit,
            # which classify_words reads off the target being odd.
            row += ["    target: 0x%x" % value, "    addend: 0"]
        row += ["    why: %s" % reason,
                "    observed:",
                "      signal: %s" % analysis.shape_signal(addr),
                "      run: %s" % run,
                "      evidence: %s" % note]
        rows.append("\n".join(row))
    block = [MARKER,
             "# One entry per word a run decided; abi/observe_words.py --apply",
             "# rewrites everything below this line.", ""] + rows
    with open(path, "w") as fh:
        fh.write(head + "\n\n" + "\n".join(block) + "\n")
    return len(rows)


def ensure_reasons(path):
    """The two reason texts the generated entries name, added once."""
    text = open(path).read()
    if "observed_address:" in text:
        return
    insert = "".join("  %s: >%s\n" % (name, body.rstrip())
                     for name, body in sorted(REASONS.items()))
    text = text.replace("\noverrides:", "\n" + insert + "\noverrides:", 1)
    open(path, "w").write(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime", required=True,
                    help="the scratch copy of renode-sim the runs happen in;"
                         " it carries renode-sim's scripts/ and models/"
                         " directories, because Renode resolves an @path"
                         " against the working directory")
    ap.add_argument("--export", default=os.path.join(HERE, "out", "ghidra"))
    ap.add_argument("--image", default=os.path.join(SIM, "appl.bin"))
    ap.add_argument("--facts", default=os.path.join(HERE, "words.yaml"))
    ap.add_argument("--emit", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--apply", action="store_true",
                    help="write the observed pointers into abi/words.yaml")
    ap.add_argument("--scenario", action="append", default=[],
                    choices=sorted(SCENARIOS))
    args = ap.parse_args()

    analysis = Analysis(args.export, args.image, args.facts)
    sites = review_sites(analysis)
    chosen = args.scenario or sorted(SCENARIOS)

    if args.emit or args.run:
        written = emit(args.runtime, sites, analysis)
        print("%d words to decide, %d load sites, %d watch targets"
              % (len(sites), len(set(s for _, l, _, _ in sites for s in l)),
                 len(set(w for _, _, w, _ in sites if w is not None))))
        for name, path, _ in written:
            print("  %s" % os.path.relpath(path, args.runtime))

    if args.run:
        for name in chosen:
            took = run_scenario(name, SCENARIOS[name], args.runtime)
            seen = parse_observation(os.path.join(args.runtime, "out", "observe",
                                                  "obs-%s.txt" % name))
            print("  %-8s %6.1f s wall, %d addresses touched"
                  % (name, took, len(seen)))

    if args.report:
        observations = {}
        for name in sorted(SCENARIOS):
            path = os.path.join(args.runtime, "out", "observe",
                                "obs-%s.txt" % name)
            if os.path.exists(path):
                observations[name] = parse_observation(path)
        if not observations:
            sys.exit("no observation files in %s; run --run first" % args.runtime)
        decided = verdicts(sites, observations)
        counts = collections.Counter(v[0] for v in decided.values())
        by_signal = collections.Counter(
            (analysis.shape_signal(w["addr"]), decided[w["addr"]][0])
            for w, _, _, _ in sites)
        print("scenarios: %s" % ", ".join(sorted(observations)))
        for bucket in ("observed_address", "observed_value_only", "never_loaded"):
            print("  %-20s %d" % (bucket, counts[bucket]))
        for (signal, bucket), n in sorted(by_signal.items()):
            print("    %-24s %-20s %d" % (signal, bucket, n))
        # The never-loaded words stay on the review list, and this is where
        # that is recorded: the runs that were driven and the fact that none of
        # them reached the word, so the next argument about one starts from a
        # scenario the rig does not have rather than from nothing.
        verdicts_path = os.path.join(args.runtime, "out", "observe",
                                     "verdicts.json")
        with open(verdicts_path, "w") as fh:
            json.dump({"scenarios": sorted(observations),
                       "counts": dict(counts),
                       "words": [{"addr": w["addr"], "value": w["value"],
                                  "signal": analysis.shape_signal(w["addr"]),
                                  "bucket": decided[w["addr"]][0],
                                  "run": decided[w["addr"]][1],
                                  "evidence": decided[w["addr"]][2]}
                                 for w, _, _, _ in sites]}, fh, indent=1)
        print("%s" % os.path.relpath(verdicts_path, args.runtime))
        if args.apply:
            ensure_reasons(args.facts)
            n = apply_to_words_yaml(args.facts, decided, analysis)
            print("abi/words.yaml: %d observed entries" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
