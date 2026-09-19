#!/usr/bin/env python3
"""The FreeRTOS objects the image creates, named by what the creates carry.

    python3 abi/kernel_objects.py             # writes the kernel class of abi/symbols.yaml
    python3 abi/kernel_objects.py --report    # the creates and what each names

A FreeRTOS create is a naming call. `xTaskCreate` is handed the entry function
and the string the kernel will print for the task; `xQueueGenericCreateStatic`
and `xQueueCreateMutexStatic` are handed the static object they will live in
and the queue type that says what kind of thing they are; all of them leave a
handle in a RAM word the rest of the module reads. Every one of those is an
argument at a call site, so the block that sets the call up is the whole
evidence, and abi/callargs.py reads it.

What the strings name:

  the task's entry function   the first argument, which is the only code the
                              string is ever about
  the handle word             the `str r0` after the call, or the out-parameter
                              `xTaskCreate` takes for it
  the static objects          the stack, the TCB and the queue storage the call
                              is handed, which belong to the object the same
                              call names

A queue and a mutex carry no string. The base of their name comes from the
module instead, strongest reading first: the log-tag module of the functions
that read the handle word, then the module of the function that creates it,
then that function's own name with the `_init` off. The boot's own tags (`M`,
`INIT`) name no module, so a create that has only those is left unnamed and
reported. The suffix is the queue type the call passes, which is the
firmware's own word for what the object is.

Nothing here guesses: an argument the block does not establish leaves its
object unnamed, and the evidence of every entry is the site and the literal it
carries.
"""

import argparse
import collections
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autonames  # noqa: E402  (the image, the disassembly and the partition)
import callargs  # noqa: E402
import symbols as symmap  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
CLASS = "kernel"

RAM_LO, RAM_HI = 0x20000000, 0x20040000

# The kernel entry points, from the `match` class of abi/symbols.yaml, with the
# number of AAPCS arguments each takes. The image links neither timers.c nor
# event_groups.c nor stream_buffer.c, so those creates have no call sites to
# mine: `vTaskStartScheduler` creates the idle task and nothing creates a timer.
CREATES = {
    "xTaskCreate": 6,
    "xTaskCreateStatic": 7,
    "xQueueGenericCreateStatic": 5,
    "xQueueCreateMutexStatic": 2,
}

# `ucQueueType` as queue.h defines it. The type is what the object is, so it is
# what the name's suffix says; a create whose type the block does not establish
# gets the neutral one, because the object is a queue either way.
QUEUE_TYPE = {0: "queue", 1: "mutex", 2: "sem", 3: "sem", 4: "recursive_mutex"}

# Tags that are the boot's own and not a module's: `[M]` is main.c's and
# `[INIT]` is every module's start-up line, so neither says whose object this is.
GENERIC_TAGS = frozenset(("M", "INIT"))


class Create(object):
    """One create call: the API, where it is, and the arguments it was given."""

    def __init__(self, api, site, fn, frame, handle):
        self.api = api
        self.site = site
        self.fn = fn
        self.frame = frame
        self.handle = handle

    def value(self, n, lo=None, hi=None):
        """Argument n as a number, or None if the block did not establish one."""
        arg = self.frame.arg(n)
        if not isinstance(arg, (callargs.Imm, callargs.Pool)):
            return None
        if lo is not None and not lo <= arg.value < hi:
            return None
        return arg.value

    @property
    def queue_type(self):
        if self.api == "xQueueCreateMutexStatic":
            return self.value(0)
        if self.api == "xQueueGenericCreateStatic":
            return self.value(4)
        return None


def find_creates(img, ex, entries):
    """Every call to one of the create APIs, with its block already read."""
    found = []
    for i, (addr, mnem, ops) in enumerate(img.insns):
        if mnem not in ("bl", "bl.w"):
            continue
        target = re.search(r"0x([0-9a-f]+)", ops.split("@")[0])
        if not target:
            continue
        api = entries.get(int(target.group(1), 16))
        if api is None:
            continue
        frame = callargs.resolve(img, img.insns, i)
        found.append(Create(api, addr, ex.owner(addr), frame,
                            returned_handle(img, i, frame)))
    return found


def returned_handle(img, call, frame):
    """The RAM word the block stores the returned handle into, if it does.

    The call returns in r0 and clobbers r0..r3 and r12, so the simulation
    carries on past it with those poisoned and the callee-saved registers as
    they were: the base of the store is usually a pool word the block loaded
    before the call. The scan ends where r0 stops being the handle.
    """
    regs = dict(frame.regs)
    for reg in ("r0", "r1", "r2", "r3", "r12"):
        regs[reg] = callargs.Unknown("clobbered by the call")
    for j in range(call + 1, min(call + 12, len(img.insns))):
        _, mnem, ops = img.insns[j]
        if mnem in callargs.BOUNDARY:
            return None
        if mnem.startswith("str"):
            field = callargs.FIELD.match(ops)
            if not field or field.group(1) != "r0":
                continue
            base = regs.get(field.group(2))
            if not isinstance(base, (callargs.Pool, callargs.Imm)):
                continue
            at = base.value + (callargs._int(field.group(3))
                               if field.group(3) else 0)
            return at if RAM_LO <= at < RAM_HI else None
            continue
        dest = callargs.DEST.match(ops)
        if not dest or not dest.group(1).startswith("r"):
            continue
        if dest.group(1) == "r0":
            return None
        regs[dest.group(1)] = callargs._value(img, regs, mnem, ops)
    return None


def slug(text):
    """A C identifier from a string the firmware chose, and nothing more."""
    out = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    return out if re.match(r"^[a-z_][a-z0-9_]*$", out or "") else None


class Modules(object):
    """Which module a function logs under, and which reads a RAM word."""

    def __init__(self, export):
        with open(os.path.join(export, "modules.json")) as fh:
            self.of_function = {int(a, 16): v["module"]
                                for a, v in json.load(fh)["functions"].items()}
        with open(os.path.join(export, "references.json")) as fh:
            refs = json.load(fh)
        self.readers = collections.defaultdict(set)
        for word in refs["words"]:
            self.readers[word["value"]].update(word["readers"])

    def named(self, fn):
        tag = self.of_function.get(fn)
        return None if tag is None or tag in GENERIC_TAGS else tag.lower()

    def of_word(self, ex, addr):
        """The module of the functions that read this word, if they agree.

        A handle is read where it is taken and given, and those are the module
        the object belongs to. Only a strict majority among the modules that
        name one claims it; a word read from nowhere, or from two modules
        equally, says nothing.
        """
        counts = collections.Counter()
        for site in self.readers.get(addr, ()):
            owner = ex.owner(site)
            tag = self.named(owner) if owner is not None else None
            if tag:
                counts[tag] += 1
        top = counts.most_common()
        if not top or (len(top) > 1 and top[0][1] == top[1][1]):
            return None
        return top[0][0]


def task_rows(create, img, modules, ex):
    """The entry function, the handle and the static buffers of a task create."""
    text = callargs.string_of(img, create.frame.arg(1), limit=40)
    if text is None:
        return [], "the name string is not a literal the block loads"
    name = slug(text.decode("latin1"))
    if name is None:
        return [], "the name string %r is not an identifier" % text
    if not name.endswith("_task") and "task" not in name:
        name += "_task"
    entry = create.value(0, autonames.APP_BASE, 0xF117C)
    if entry is None:
        return [], "the entry pointer is not a literal the block loads"
    carried = ("the name string %r and the entry pointer 0x%x that"
               " %s at 0x%x was given" % (text.decode("latin1"), entry,
                                          create.api, create.site))
    rows = [dict(address=entry & ~1, name=name, kind="function",
                 module=modules.named(create.fn), evidence=carried)]
    handle = (create.handle if create.api == "xTaskCreateStatic"
              else create.value(5, RAM_LO, RAM_HI))
    slots = [(handle, "handle", "the word the created task's handle is stored in")]
    if create.api == "xTaskCreateStatic":
        slots += [(create.value(5, RAM_LO, RAM_HI), "stack",
                   "the stack buffer the call was handed"),
                  (create.value(6, RAM_LO, RAM_HI), "tcb",
                   "the task control block the call was handed")]
    for at, suffix, why in slots:
        if at is None:
            continue
        rows.append(dict(address=at, name="%s_%s" % (name, suffix),
                         kind="global", module=modules.named(create.fn),
                         evidence="%s; %s" % (why, carried)))
    return rows, None


class Tasks(object):
    """The tasks this run named: by their entry, and by who created them."""

    def __init__(self):
        self.by_entry = {}
        self.by_creator = collections.defaultdict(set)

    def add(self, creator, entry, name):
        self.by_entry[entry] = name
        self.by_creator[creator].add(name)

    def get(self, entry):
        return self.by_entry.get(entry)

    def made_by(self, fn):
        return self.by_creator.get(fn, ())


def queue_base(create, modules, ex, tasks):
    """Which module's object this is, strongest reading first, and how it is read.

    The handle's readers are the take and the give, so they are the module that
    uses the object; the creating function's own tag is the next best thing;
    a creating function whose name says it is an initialiser names the module
    it initialises; and a creating function that is itself a task body is named
    by the string its own create carried.
    """
    if create.handle is not None:
        tag = modules.of_word(ex, create.handle)
        if tag:
            return tag, "the module of the functions that read the handle word"
    tag = modules.named(create.fn)
    if tag:
        return tag, "the module the creating function logs under"
    held = ex.fns.get(create.fn, {}).get("name", "")
    if held.endswith("_init"):
        return (re.sub(r"(_module)?_init$", "", held),
                "the name of the creating function, %s" % held)
    task = tasks.get(create.fn)
    if task:
        return (re.sub(r"_task$", "", task),
                "the creating function is the %s body" % task)
    # A start-up function that creates one task and the queues that task lives
    # on is that task's, and the string its own create carries is the only name
    # anything in it has.
    made = set(tasks.made_by(create.fn))
    if len(made) == 1:
        task = made.pop()
        return (re.sub(r"_task$", "", task),
                "the only task the creating function creates is %s" % task)
    return None, None


def queue_rows(create, img, modules, ex, taken, tasks):
    """The handle and the static object of a queue, semaphore or mutex create."""
    kind = QUEUE_TYPE.get(create.queue_type, "queue")
    base, how = queue_base(create, modules, ex, tasks)
    if base is None:
        return [], ("neither the handle's readers nor the creating function"
                    " 0x%x names a module" % (create.fn or 0))
    name = base if base.endswith("_" + kind) else "%s_%s" % (base, kind)
    taken[name] = taken.get(name, 0) + 1
    if taken[name] > 1:
        name = "%s_%d" % (name, taken[name] - 1)
    carried = ("%s at 0x%x with queue type %s; the name's module is %s"
               % (create.api, create.site,
                  create.queue_type if create.queue_type is not None
                  else "the block does not establish", how))
    rows = []
    if create.handle is not None:
        rows.append(dict(address=create.handle, name=name, kind="global",
                         module=base, evidence="the handle word: " + carried))
    statics = [(create.value(1, RAM_LO, RAM_HI), "static")] \
        if create.api == "xQueueCreateMutexStatic" else \
        [(create.value(3, RAM_LO, RAM_HI), "static"),
         (create.value(2, RAM_LO, RAM_HI), "storage")]
    for at, suffix in statics:
        if at is None:
            continue
        rows.append(dict(address=at, name="%s_%s" % (name, suffix),
                         kind="global", module=base,
                         evidence="the static object handed to " + carried))
    if not rows:
        return [], "the block establishes neither the handle nor the static object"
    return rows, None


def collect(img, ex, modules, creates, kernel_bodies):
    rows, refused, taken, tasks = [], [], {}, Tasks()
    # The tasks first: a queue created inside a task's own body takes its name,
    # so the task names have to exist before the queues are read.
    # xQueueCreateMutexStatic is itself a call to the generic create, and the
    # kernel's own internals are not the image's objects.
    mine = [c for c in sorted(creates, key=lambda c: c.site)
            if c.fn not in kernel_bodies]
    for create in mine:
        if not create.api.startswith("xTask"):
            continue
        made, why = task_rows(create, img, modules, ex)
        rows += made
        for row in made:
            if row["kind"] == "function":
                tasks.add(create.fn, row["address"], row["name"])
        if why:
            refused.append((create.site, create.api, why))
    for create in mine:
        if create.api.startswith("xTask"):
            continue
        made, why = queue_rows(create, img, modules, ex, taken, tasks)
        rows += made
        if why:
            refused.append((create.site, create.api, why))
    return rows, refused


def deduplicate(rows, refused):
    """One address, one name.

    Two creates that give one address two names do not make one of them the
    answer: the image creates the same body under both strings, so the address
    is left unnamed and both readings are reported.
    """
    at = collections.defaultdict(list)
    for row in rows:
        at[row["address"]].append(row)
    out, by_name = [], {}
    for address, claims in sorted(at.items()):
        names = sorted(set(c["name"] for c in claims))
        if len(names) > 1:
            refused.append((address, "two creates",
                            "0x%x is %s" % (address, " and ".join(names))))
            continue
        row = claims[0]
        if row["name"] in by_name:
            refused.append((address, "two addresses",
                            "%s is 0x%x and 0x%x"
                            % (row["name"], by_name[row["name"]], address)))
            continue
        by_name[row["name"]] = address
        out.append(row)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default=os.path.join(SIM, "appl.bin"))
    ap.add_argument("--dis", default=os.path.join(SIM, "out", "appl.dis"))
    ap.add_argument("--export", default=os.path.join(HERE, "out", "ghidra"))
    ap.add_argument("--report", action="store_true",
                    help="print the creates and what they name; write nothing")
    args = ap.parse_args()

    prior = symmap.load()
    entries = {}
    for api in CREATES:
        if api not in prior.by_name:
            sys.exit("abi/kernel_objects.py: abi/symbols.yaml does not name %s" % api)
        entries[prior.by_name[api].address] = api

    img = autonames.Image(args.image, args.dis)
    ex = autonames.Export(args.export)
    if not ex.ok:
        sys.exit("abi/kernel_objects.py: no export under %s" % args.export)
    modules = Modules(args.export)
    creates = find_creates(img, ex, entries)
    rows, refused = collect(img, ex, modules, creates, set(entries))
    rows = deduplicate(rows, refused)

    # A create's argument is what the object is, so these settle their
    # addresses; where the map already names one by hand the hand entry is the
    # duplicate and `--check` says so.
    for row in rows:
        row["class"] = CLASS
    counts = collections.Counter(c.api for c in creates)
    # A name the map already carries for one of these addresses is a reading to
    # compare against: a `prose` label does not stop a derivation, so where the
    # two differ the difference is printed rather than lost in the rewrite.
    for row in sorted(rows, key=lambda r: r["address"]):
        held = prior.by_address.get(row["address"])
        if held is not None and held.name != row["name"]:
            print("  0x%x is %s (%s) in the map and %s here"
                  % (row["address"], held.name, held.klass, row["name"]))
    if args.report:
        for row in sorted(rows, key=lambda r: r["address"]):
            print("0x%08x %-34s %-8s %s" % (row["address"], row["name"],
                                            row["kind"], row["evidence"]))
    print("%d create sites (%s); %d names"
          % (len(creates), ", ".join("%s %d" % kv for kv in sorted(counts.items())),
             len(rows)))
    for site, api, why in refused:
        print("  0x%x %-26s %s" % (site, api, why))
    if args.report:
        return 0
    try:
        added = symmap.load().rewrite(rows, {CLASS},
                                      verified=set(r["address"] for r in rows))
    except symmap.Refusal as err:
        sys.exit("abi/kernel_objects.py: abi/symbols.yaml: %s" % err)
    print("wrote %d entries to abi/symbols.yaml" % added)
    return 0


if __name__ == "__main__":
    sys.exit(main())
