#!/usr/bin/env python3
"""What each root of the app reaches, and what only it reaches.

    python3 abi/reach.py [--rebuild] [--json out/reach/reach.json]

The relocatable object abi/blobify.py emits is the reference graph: every call,
every tail call and every word the classification calls a pointer is a
relocation there, so "section A references section B" is a fact of the object
rather than a reading of the disassembly. Sections that the object leaves
unconnected -- a dispatch table nothing loads the address of, a task function
that only xTaskCreateStatic's argument names -- are exactly the roots, and
abi/roots.yaml says where each one comes from.

The answer this produces is the cost of a feature: the bytes a root reaches
that no other root reaches can be removed with it, and the bytes it shares say
what removing it would not buy.

The object is built into out/reach/ with its own placement so that a layout
experiment left in out/relink/ cannot be mistaken for the stock addresses.
"""

import argparse
import bisect
import collections
import json
import os
import re
import struct
import subprocess
import sys

import yaml

from elftools.elf.elffile import ELFFile

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
OUT = os.path.join(SIM, "out", "reach")
APP_BASE = 0x27000
R_ARM_ABS32 = 2


class Image(object):
    """The app as a graph of sections, with the address map to look roots up."""

    def __init__(self, names, addrs, sizes, kinds, edges, relocs, undefined):
        self.names, self.addrs, self.sizes = names, addrs, sizes
        self.kinds, self.edges, self.relocs = kinds, edges, relocs
        self.undefined = undefined
        self.order = sorted(range(len(names)), key=lambda i: addrs[i])
        self.starts = [addrs[i] for i in self.order]

    def at(self, addr):
        """The section holding `addr`, or None if it is outside the image."""
        i = bisect.bisect_right(self.starts, addr) - 1
        if i < 0:
            return None
        s = self.order[i]
        return s if addr < self.addrs[s] + self.sizes[s] else None

    def bytes_of(self, sections):
        code = sum(self.sizes[i] for i in sections if self.kinds[i] == "code")
        return code, sum(self.sizes[i] for i in sections) - code

    def unnamed_bytes(self, sections, kind=None):
        """Bytes in sections whose name is still the export's placeholder.

        `FUN_` and `block_` are code Ghidra never named, so the code figure
        says how much naming work a feature would pay for; the data
        placeholders are strings and untyped runs and are counted separately.
        """
        return sum(self.sizes[i] for i in sections
                   if (kind is None or self.kinds[i] == kind)
                   and re.search(r"\.(FUN_|block_|gap_|sliver_|pool_|DAT_|caseD"
                                 r"|ptrtab_|s_|u_)", self.names[i]))

    def total(self):
        return self.bytes_of(range(len(self.names)))


def build_object(obj, place, rebuild):
    if not rebuild and os.path.exists(obj) and os.path.exists(place):
        return
    subprocess.check_call(
        [sys.executable, os.path.join(HERE, "blobify.py"), "-o", obj,
         "--place", place, "--stock", os.path.join(OUT, "stock-defs.o")],
        stdout=subprocess.DEVNULL)


def load_object(obj, place):
    """The section graph: one node per section, one edge per relocation."""
    placed = {}
    for line in open(place):
        m = re.search(r"KEEP\(\*[\w.-]+\((\S+)\)\)\s+/\* 0x([0-9a-f]+)", line)
        if m:
            placed[m.group(1)] = int(m.group(2), 16)
    elf = ELFFile(open(obj, "rb"))
    secs = list(elf.iter_sections())
    index = {s.name: i for i, s in enumerate(secs)}
    symbols = list(elf.get_section_by_name(".symtab").iter_symbols())
    owner = {}
    for sym in symbols:
        if sym.name and isinstance(sym.entry.st_shndx, int):
            owner.setdefault(sym.name, sym.entry.st_shndx)

    names, addrs, sizes, kinds, remap = [], [], [], [], {}
    for i, s in enumerate(secs):
        if s["sh_type"] != "SHT_PROGBITS" or s.name not in placed:
            continue
        remap[i] = len(names)
        names.append(s.name)
        addrs.append(placed[s.name])
        sizes.append(s["sh_size"])
        kinds.append("code" if s.name.startswith(".text.") else "data")
    edges = [set() for _ in names]
    relocs = [[] for _ in names]
    undefined = collections.Counter()
    for s in secs:
        if not s.name.startswith(".rel.") or s.name[4:] not in index:
            continue
        src = remap.get(index[s.name[4:]])
        if src is None:
            continue
        for r in s.iter_relocations():
            name = symbols[r["r_info_sym"]].name
            target = owner.get(name)
            if target is None or remap.get(target) is None:
                undefined[name] += 1
                continue
            edges[src].add(remap[target])
            relocs[src].append((r["r_offset"], remap[target], r["r_info_type"]))
    if len(placed) != len(names):
        raise SystemExit("%d placements but %d sections" % (len(placed), len(names)))
    return Image(names, addrs, sizes, kinds, edges, relocs, undefined)


def reach(image, start):
    seen, stack = set(start), list(start)
    while stack:
        for nxt in image.edges[stack.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


class Export(object):
    """The inputs the root rules read, loaded once."""

    def __init__(self):
        self.image = open(os.path.join(SIM, "appl.bin"), "rb").read()
        with open(os.path.join(HERE, "out", "ghidra", "references.json")) as fh:
            self.references = json.load(fh)
        with open(os.path.join(HERE, "out", "ghidra", "items.json")) as fh:
            self.function_starts = set(f["start"] for f in json.load(fh)["functions"])
        with open(os.path.join(HERE, "autonames.yaml")) as fh:
            self.autonames = yaml.safe_load(fh)["functions"]
        with open(os.path.join(HERE, "boundary.yaml")) as fh:
            boundary = yaml.safe_load(fh)
        self.library = [tuple(r) for r in boundary["library_ranges"]]
        with open(os.path.join(HERE, "out", "ghidra", "words.json")) as fh:
            self.words = json.load(fh)
        with open(os.path.join(HERE, "hwa10.yaml")) as fh:
            self.manifest = yaml.safe_load(fh)
        self.named = {f["address"]: f["name"] for f in self.autonames}
        self.tables = {t["name"]: t for t in self.manifest["tables"]}
        self.structs = {t["name"]: t for t in self.manifest["table_structs"]}
        self.functions = {f["name"]: f["address"] for f in self.manifest["functions"]}

    def word(self, addr):
        return struct.unpack_from("<I", self.image, addr - APP_BASE)[0]

    def half(self, addr):
        return struct.unpack_from("<H", self.image, addr - APP_BASE)[0]

    def string(self, addr):
        off = addr - APP_BASE
        if not 0 <= off < len(self.image):
            return ""
        end = self.image.index(b"\0", off)
        return self.image[off:end].decode("latin1").strip()

    def in_image(self, addr):
        return APP_BASE <= addr < APP_BASE + len(self.image)


def rule_vector_table(export, image, group, spec):
    """One root per vector-table entry, named by the symbol the object relocates.

    The table is the image's one fixed point: the MBR boots the app through it
    and the SoftDevice forwards every interrupt through it, so nothing in the
    image needs to reference it and nothing does.
    """
    section = image.at(APP_BASE)
    roots = []
    for offset, target, kind in sorted(image.relocs[section]):
        if kind != R_ARM_ABS32 or offset >= 0x100:
            continue
        roots.append(Root(group, "vector_%d" % (offset // 4), image.addrs[target],
                          "vector table word 0x%08x, IRQ %d"
                          % (APP_BASE + offset, offset // 4 - 16), target))
    return roots


def pcrel_load(export, site, register, limit=96):
    """The word a `ldr r<register>, [pc, #imm]` before `site` reads.

    The task entry and the task name are both literal-pool loads a few
    instructions before the call, and the encoding is the T1 one (0x48xx), so
    the scan back from the call site is over halfwords and reads the pool word
    itself rather than trusting a decompilation of the argument.
    """
    for back in range(2, limit, 2):
        at = site - back
        half = export.half(at)
        if (half >> 11) == 0x09 and ((half >> 8) & 7) == register:
            pool = ((at + 4) & ~3) + (half & 0xFF) * 4
            return at, pool, export.word(pool)
    return None, None, None


def rule_tasks(export, image, group, spec):
    """One root per task created at boot: the entry each create call passes in r0.

    xTaskCreateStatic takes the entry in r0 and the name in r1; both come from
    the caller's literal pool, so the call site names the task as well as its
    body. A task function is a root because nothing branches to it: the kernel
    enters it through the stack frame prvInitialiseNewTask builds. The pool word
    is the entry to cut, because it is the only reference to the task body and
    dropping the task means dropping the create call that reads it.
    """
    creators = {export.functions["xTaskCreateStatic"]: "xTaskCreateStatic",
                0x9E410: "xTaskCreate"}
    roots = []
    for call in sorted(export.references["calls"], key=lambda c: c["from"]):
        if call["to"] not in creators or call["external"]:
            continue
        load, pool, entry = pcrel_load(export, call["from"], 0)
        if entry is None or not entry & 1 or not export.in_image(entry):
            raise SystemExit("no task entry for the create call at 0x%x" % call["from"])
        _, _, namearg = pcrel_load(export, call["from"], 1, call["from"] - load)
        label = export.string(namearg) if namearg and export.in_image(namearg) else ""
        roots.append(Root(
            group, "task_%s" % (label or "%x" % (entry & ~1)), entry & ~1,
            "%s at 0x%x in 0x%x, entry from the pool word 0x%x%s"
            % (creators[call["to"]], call["from"], call["function"], pool,
               ", name %r" % label if label else ""), None, image, entry=pool))
    return roots


def rule_command_table(export, image, group, spec, table, prefix):
    """One root per function slot of a dispatch-table row.

    A row is a root because the walker reaches the table without a reference the
    object can see (hwa10.yaml: no literal pool holds the wpp table's address),
    so per-command exclusivity is only computable if each row stands alone. The
    columns come from the row struct the manifest declares, so the WPP table's
    {id, handler, name} and the shell's {name, run, help} read the same way.
    """
    spec = spec.get("table") or export.tables[table]
    fields = export.structs[spec["entry"]]["fields"]
    roots = []
    for i in range(spec["count"]):
        row = spec["address"] + i * spec["stride"]
        cols = {fname: export.word(row + 4 * j)
                for j, (_, fname) in enumerate(fields)}
        types = {fname: ftype for ftype, fname in fields}
        key = next((cols[f] for f, t in types.items() if t == "u32"), None)
        label = next((export.string(cols[f]) for f, t in types.items()
                      if t == "const char *"), None)
        slots = [f for f, t in types.items() if t == "void *"]
        for fname in slots:
            handler = cols[fname]
            if not handler & 1 or not export.in_image(handler):
                raise SystemExit("%s row 0x%x holds no handler in %s"
                                 % (table, row, fname))
            entry = handler & ~1
            name = export.named.get(entry)
            if not name:
                name = label or "%s_%x" % (prefix, entry)
                if len(slots) > 1:
                    name = "%s_%s" % (name, fname)
            roots.append(Root(
                group, name, entry,
                "%s row 0x%08x field %s = 0x%08x: key %s, label %r"
                % (table, row, fname, handler, key, label),
                None, image, command=key, entry=row))
    return roots


def rule_device_table(export, image, group, spec, table):
    """One root per function slot of a device row, named by the row's address."""
    spec = export.tables[table]
    fields = export.structs[spec["entry"]]["fields"]
    roots = []
    for i in range(spec["count"]):
        row = spec["address"] + i * spec["stride"]
        offset = 0
        for ftype, fname in fields:
            value = export.word(row + offset) if ftype != "u16" else 0
            offset += 2 if ftype == "u16" else 4
            if ftype != "void *" or fname == "bus" or not value & 1:
                continue
            roots.append(Root(
                group, "%s_%x_%s" % (table.split("_")[0], row, fname), value & ~1,
                "%s row 0x%08x field %s = 0x%08x" % (table, row, fname, value),
                None, image, entry=row + offset - 4))
    return roots


def rule_orphan_tables(export, image, group, reached, rooted):
    """Every pointer table in rodata that nothing reaches, as its own root.

    A table of handler addresses that no instruction loads the address of is
    read by something the object cannot show -- a walker that computes the
    address, or a copy made into RAM at boot. Leaving them out would call their
    handlers dead, so each such table is a root; the iteration is needed because
    one orphan table can reach another.

    An item whose pointers all name sections that already carry a root is not a
    root: a dispatch table's row is cut into one section per word, so every row
    of the WPP and shell tables is an unreferenced item holding exactly one
    handler address, and making each of those a root would give every command a
    second root and leave no command owning its own handler.
    """
    roots, seen = [], set(reached)
    while True:
        found = []
        for i, name in enumerate(image.names):
            if i in seen or image.kinds[i] != "data":
                continue
            pointers = [t for _, t, kind in image.relocs[i]
                        if kind == R_ARM_ABS32 and image.kinds[t] == "code"]
            if pointers and not all(t in rooted for t in pointers):
                found.append((i, len(pointers)))
        if not found:
            return roots
        for i, pointers in found:
            roots.append(Root(group, image.names[i][len(".rodata."):], image.addrs[i],
                              "%d code pointers in a rodata item nothing references"
                              % pointers, i))
        seen |= reach(image, [i for i, _ in found])


class Root(object):
    """A place the image is entered, and the edge that entry would cost.

    A table row is a root and is also referenced: the partition merges a run of
    rows into the section of the function they sit next to, so the object holds
    an edge from that section to the handler. Removing the row means removing
    the root and that edge together, which is what `cuts` carries; without it a
    command's cost would exclude its own handler, because the table section
    still names it.
    """

    def __init__(self, group, name, addr, evidence, section=None, image=None,
                 command=None, entry=None):
        self.group, self.name, self.addr = group, name, addr
        self.evidence, self.command, self.entry = evidence, command, entry
        self.section = section if section is not None else image.at(addr)
        if self.section is None:
            raise SystemExit("root %s at 0x%x is outside the image" % (name, addr))
        self.cuts = set()
        if entry is not None and image is not None:
            row = image.at(entry)
            if row is not None and self.section in image.edges[row]:
                self.cuts.add((row, self.section))


RULES = {
    "vector_table": rule_vector_table,
    "tasks": rule_tasks,
    "wpp_cmd_table":
        lambda e, i, g, s: rule_command_table(e, i, g, s, "wpp_cmd_table", "wpp"),
    "shell_cmd_table":
        lambda e, i, g, s: rule_command_table(e, i, g, s, "shell_cmd_table", "shell"),
    "i2c_device_table":
        lambda e, i, g, s: rule_device_table(e, i, g, s, "i2c_device_table"),
    "spi_device_table":
        lambda e, i, g, s: rule_device_table(e, i, g, s, "spi_device_table"),
}


def collect_roots(spec, export, image):
    """Every root of roots.yaml, in group order; the orphan rule runs last.

    The orphan rule is defined against what everything else already reaches, so
    it can only be evaluated once the rest of the root set exists.
    """
    roots, deferred = [], []
    for group in spec["groups"]:
        name, rule = group["group"], group.get("derive")
        if rule == "orphan_pointer_tables":
            deferred.append(group)
            continue
        if rule:
            roots += RULES[rule](export, image, name, group)
        for row in group.get("roots", []):
            entry = row.get("entry")
            roots.append(Root(name, row["name"], int(str(row["address"]), 0),
                              row["evidence"], None, image,
                              entry=int(str(entry), 0) if entry else None))
    for group in deferred:
        roots += rule_orphan_tables(
            export, image, group["group"], reach(image, [r.section for r in roots]),
            {r.section for r in roots})
    return roots


def check_entry_roots(export, roots):
    """Refuse a root the partition does not call a function start.

    Every group but the orphan tables names a place execution enters: a vector
    word, a task entry, a dispatch row's handler. If the partition does not have
    a function starting there, one of the two is wrong -- the idle task's entry
    was six bytes below where the body match anchored it, so the relayout was
    free to separate its prologue from its body -- and a gc link would cut on
    the wrong boundary. The orphan-table group is exempt: its roots are the
    tables themselves.
    """
    bad = [r for r in roots if r.group != "orphan_pointer_tables"
           and r.addr not in export.function_starts]
    if not bad:
        return
    for r in bad:
        print("root %s/%s at 0x%08x is not a function start: %s"
              % (r.group, r.name, r.addr, r.evidence))
    raise SystemExit("%d entry roots are not function starts" % len(bad))


def removal(image, roots, baseline, dropped):
    """The sections that stop being reachable when `dropped` roots are removed.

    This is the only honest measure of a root's cost. Counting how many roots
    reach a section understates it wherever the root is also referenced -- a
    table row's handler is named by the section the rows were merged into, so
    the handler would never count as anyone's -- and it says nothing about a
    feature entered by a branch rather than by a root. Dropping the roots,
    cutting the edges that enter them and walking again answers both.
    """
    cuts = set().union(*(roots[i].cuts for i in dropped)) if dropped else set()
    kept = [r.section for i, r in enumerate(roots) if i not in dropped]
    return baseline - reach_without(image, kept, cuts)


def reach_without(image, starts, cuts):
    """Reachability with a set of (section, section) edges removed."""
    seen, stack = set(starts), list(starts)
    while stack:
        at = stack.pop()
        for nxt in image.edges[at]:
            if (at, nxt) in cuts or nxt in seen:
                continue
            seen.add(nxt)
            stack.append(nxt)
    return seen


def explain_dead(image, export, dead):
    """Why nothing reaches a section: a dead copy, a review word, or no word.

    Three reasons, and they are different problems. The blob carries its own
    copy of the kernel that the boundary relocations bypass, so those sections
    are dead by construction and the relink already drops them. A section a
    word of the image names, where the classification called that word a
    constant or left it for review, is not dead: it is the review list, and the
    root set is missing whatever reads that word. Everything else has no word
    naming it at all.
    """
    undecided = collections.defaultdict(list)
    for row in export.words["words"]:
        if row["class"] == "pointer":
            continue
        # A word the classification did not call a pointer has no target
        # recorded, so the value is what has to be read as an address.
        section = image.at(row["value"] & ~1)
        if section is not None:
            undecided[section].append(row)
    reasons = collections.Counter()
    bytes_by_reason = collections.Counter()
    detail = collections.defaultdict(list)
    for i in dead:
        if any(lo <= image.addrs[i] < hi for lo, hi in export.library):
            reason = "library copy the boundary bypasses"
        elif i in undecided:
            reason = "named by a word still classified %s" % \
                undecided[i][0]["class"]
        else:
            reason = "no word of the image names it"
        reasons[reason] += 1
        bytes_by_reason[reason] += image.sizes[i]
        detail[reason].append(("0x%08x" % image.addrs[i], image.sizes[i],
                               image.names[i]))
    return reasons, bytes_by_reason, detail


def reach_inside(image, starts, allowed):
    """Reachability restricted to a set of sections."""
    seen = {s for s in starts if s in allowed}
    stack = list(seen)
    while stack:
        for nxt in image.edges[stack.pop()]:
            if nxt in allowed and nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def feature_report(image, roots, reached, feature, baseline):
    """What removing a feature would take with it, and what it would leave.

    A feature is not a root set: the WPPS record path is a branch inside the
    GATT write demux, so the BLE task reaches the whole tunnel and nothing in
    it is exclusive to any root. What a removal really is, is the feature's
    roots dropped and the call sites that enter it cut, so that is what this
    measures: reachability from every remaining root with those edges gone, and
    the difference against the whole-image reach is the feature's cost. The
    edges are the answer to "what would the removal have to edit".
    """
    removed = set()
    for addr in feature.get("remove_roots", []):
        section = image.at(int(str(addr), 0))
        removed |= {i for i, r in enumerate(roots) if r.section == section}
    for group in feature.get("remove_groups", []):
        removed |= {i for i, r in enumerate(roots) if r.group == group}
    if not removed and not feature.get("cut_edges"):
        raise SystemExit("feature %s removes nothing" % feature["feature"])
    cuts = set()
    for edge in feature.get("cut_edges", []):
        src, dst = image.at(int(str(edge["from"]), 0)), image.at(int(str(edge["to"]), 0))
        if src is None or dst is None or dst not in image.edges[src]:
            raise SystemExit("%s: no edge 0x%s -> 0x%s in the object"
                             % (feature["feature"], edge["from"], edge["to"]))
        cuts.add((src, dst))
    cuts |= set().union(*(roots[i].cuts for i in removed)) if removed else set()
    kept = [r.section for i, r in enumerate(roots) if i not in removed]
    after = reach_without(image, kept, cuts)
    gone = baseline - after
    # What the feature can reach at all: from its own roots and from the far
    # end of every cut, which is where the feature is entered from code that
    # stays. The part of it that survives the removal is what it shares.
    inside = reach(image, [dst for _, dst in cuts])
    for i in removed:
        inside |= reached[i]
    code, data = image.bytes_of(gone)
    shared_code, shared_data = image.bytes_of(inside & after)
    report = {
        "feature": feature["feature"],
        "evidence": feature["evidence"],
        "removed_roots": sorted(roots[i].name for i in removed),
        "cut_edges": feature.get("cut_edges", []),
        "gone_sections": len(gone), "gone_code": code, "gone_data": data,
        "gone_unnamed": image.unnamed_bytes(gone),
        "gone_unnamed_code": image.unnamed_bytes(gone, "code"),
        "kept_shared_code": shared_code, "kept_shared_data": shared_data,
        "gone_list": sorted(("0x%08x" % image.addrs[s], image.sizes[s], image.names[s])
                            for s in gone),
        "kept_shared_top": sorted(
            (("0x%08x" % image.addrs[s], image.sizes[s], image.names[s])
             for s in inside & after), key=lambda r: -r[1])[:20],
    }
    for name, addresses in (feature.get("splits") or {}).items():
        # The walk stays inside what the removal drops: every piece of the
        # tunnel also calls the shared core, so a plain reach from the piece
        # would claim the whole feature back.
        part = reach_inside(image, [image.at(int(str(a), 0)) for a in addresses], gone)
        pcode, pdata = image.bytes_of(part)
        report.setdefault("splits", {})[name] = {
            "sections": len(part), "code": pcode, "data": pdata,
            "list": sorted(("0x%08x" % image.addrs[s], image.sizes[s], image.names[s])
                           for s in part)}
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--obj", default=os.path.join(OUT, "appl-blob.o"))
    ap.add_argument("--place", default=os.path.join(OUT, "place.ld"))
    ap.add_argument("--roots", default=os.path.join(HERE, "roots.yaml"))
    ap.add_argument("--json", default=os.path.join(OUT, "reach.json"))
    ap.add_argument("--rebuild", action="store_true",
                    help="re-cut the object even if out/reach already holds one")
    ap.add_argument("--top", type=int, default=30)
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    build_object(args.obj, args.place, args.rebuild)
    image = load_object(args.obj, args.place)
    export = Export()
    with open(args.roots) as fh:
        spec = yaml.safe_load(fh)
    roots = collect_roots(spec, export, image)
    check_entry_roots(export, roots)
    reached = [reach(image, [r.section]) for r in roots]
    group_seen = collections.defaultdict(set)
    group_roots = collections.defaultdict(set)
    for i, root in enumerate(roots):
        group_seen[root.group] |= reached[i]
        group_roots[root.group].add(i)

    total_code, total_data = image.total()
    union = set().union(*group_seen.values())
    union_code, union_data = image.bytes_of(union)
    dead = [i for i in range(len(image.names)) if i not in union]
    dead_code, dead_data = image.bytes_of(dead)

    print("%d sections, %d code bytes, %d data bytes"
          % (len(image.names), total_code, total_data))
    print("%d roots in %d groups reach %d sections: %d code, %d data (%.1f%% of the image)"
          % (len(roots), len(group_seen), len(union), union_code, union_data,
             100.0 * (union_code + union_data) / (total_code + total_data)))
    print("%d sections no root reaches: %d code, %d data"
          % (len(dead), dead_code, dead_data))
    vector_only = reach(image, [image.at(APP_BASE)])
    gc_code, gc_data = image.bytes_of(vector_only)
    print("from the vector table alone (what --gc-sections keeps today):"
          " %d sections, %d code, %d data"
          % (len(vector_only), gc_code, gc_data))

    print("\nper group")
    groups = {}
    for group, seen in sorted(group_seen.items()):
        own = removal(image, roots, union, group_roots[group])
        code, data = image.bytes_of(seen)
        ecode, edata = image.bytes_of(own)
        groups[group] = {
            "roots": len(group_roots[group]),
            "reached_sections": len(seen), "code": code, "data": data,
            "exclusive_sections": len(own), "exclusive_code": ecode,
            "exclusive_data": edata, "exclusive_unnamed": image.unnamed_bytes(own),
            "exclusive_unnamed_code": image.unnamed_bytes(own, "code"),
            "shared_top": sorted(
                (("0x%08x" % image.addrs[x], image.sizes[x], image.names[x])
                 for x in seen - own), key=lambda r: -r[1])[:20],
        }
        print("  %-22s %4d roots  reach %5d sections %7d B  removing them drops"
              " %5d sections %7d B (%d B unnamed, %d of it code)"
              % (group, groups[group]["roots"], len(seen), code + data, len(own),
                 ecode + edata, groups[group]["exclusive_unnamed"],
                 groups[group]["exclusive_unnamed_code"]))

    rows = []
    for i, root in enumerate(roots):
        own = removal(image, roots, union, {i})
        code, data = image.bytes_of(reached[i])
        ecode, edata = image.bytes_of(own)
        rows.append({
            "group": root.group, "name": root.name,
            "addr": "0x%08x" % root.addr, "command": root.command,
            "evidence": root.evidence, "reached_sections": len(reached[i]),
            "code": code, "data": data, "exclusive_sections": len(own),
            "exclusive_code": ecode, "exclusive_data": edata,
            "exclusive_unnamed": image.unnamed_bytes(own),
            "exclusive_unnamed_code": image.unnamed_bytes(own, "code"),
            "exclusive_list": sorted(
                ("0x%08x" % image.addrs[x], image.sizes[x], image.names[x])
                for x in own),
        })

    for group in ("wpp_cmd_table", "shell_cmd_table"):
        top = sorted((r for r in rows if r["group"] == group),
                     key=lambda r: -(r["exclusive_code"] + r["exclusive_data"]))
        if not top:
            continue
        print("\ntop %d by removal cost in %s" % (args.top, group))
        for r in top[:args.top]:
            print("  %-34s %-9s reach %6d B  drops %6d B (%d code, %d data)"
                  % (r["name"], r["command"] if r["command"] else "",
                     r["code"] + r["data"],
                     r["exclusive_code"] + r["exclusive_data"],
                     r["exclusive_code"], r["exclusive_data"]))

    features = []
    for feature in spec.get("features", []):
        report = feature_report(image, roots, reached, feature, union)
        features.append(report)
        print("\nfeature %s: removing %d roots and cutting %d call sites drops"
              " %d sections, %d code + %d data B (%d B still unnamed);"
              " it keeps %d code + %d data B it shares"
              % (report["feature"], len(report["removed_roots"]),
                 len(report["cut_edges"]), report["gone_sections"],
                 report["gone_code"], report["gone_data"], report["gone_unnamed"],
                 report["kept_shared_code"], report["kept_shared_data"]))
        for a, size, name in report["kept_shared_top"][:5]:
            print("  shares %s %6d B %s" % (a, size, name))
        for name, part in sorted(report.get("splits", {}).items()):
            print("  of which %-24s %4d sections, %6d code + %5d data B"
                  % (name, part["sections"], part["code"], part["data"]))

    checks = []
    for check in spec.get("sanity", []):
        addr = int(str(check["address"]), 0)
        section = image.at(addr)
        ok = section in union
        checks.append({"name": check["name"], "addr": "0x%08x" % addr,
                       "section": image.names[section], "reached": ok})
        if not ok:
            print("SANITY: %s (0x%x) is reached by no root" % (check["name"], addr))
    reset = spec["reset_reaches"]
    start = image.at(int(str(reset["from"]), 0))
    target = image.at(int(str(reset["to"]), 0))
    reset_ok = target in reach(image, [start])
    checks.append({"name": "reset_handler_reaches_task_creation",
                   "addr": "0x%x" % int(str(reset["to"]), 0),
                   "section": image.names[target],
                   "reached": reset_ok})
    print("\nsanity: %d/%d known-executed functions reached; 0x%x -> 0x%x: %s"
          % (sum(1 for c in checks if c["reached"]), len(checks),
             int(str(reset["from"]), 0), int(str(reset["to"]), 0),
             "yes" if reset_ok else "NO"))

    dead_rows = sorted(("0x%08x" % image.addrs[s], image.sizes[s], image.names[s])
                       for s in dead)
    reasons, dead_bytes, dead_detail = explain_dead(image, export, dead)
    print("\nwhy nothing reaches a section")
    for reason, count in reasons.most_common():
        print("  %-44s %5d sections %7d B" % (reason, count, dead_bytes[reason]))
    print("largest sections no root reaches")
    for a, size, name in sorted(dead_rows, key=lambda r: -r[1])[:15]:
        print("  %s %6d %s" % (a, size, name))

    with open(args.json, "w") as fh:
        json.dump({
            "object": os.path.relpath(args.obj, SIM),
            "image": {"sections": len(image.names), "code": total_code,
                      "data": total_data},
            "union": {"sections": len(union), "code": union_code,
                      "data": union_data},
            "dead": {"sections": len(dead), "code": dead_code, "data": dead_data,
                     "reasons": {r: {"sections": reasons[r], "bytes": dead_bytes[r],
                                     "list": sorted(dead_detail[r])}
                                 for r in reasons},
                     "list": dead_rows},
            # The addresses, not just the count: abi/blobify.py --gc leaves a
            # root out of its KEEP list when the fixed points already reach it,
            # so that the list says how much of the root set is really
            # unreferenced rather than repeating what gc would keep anyway.
            "gc_sections": {"sections": len(vector_only),
                            "code": gc_code, "data": gc_data,
                            "addrs": sorted(image.addrs[i] for i in vector_only)},
            "groups": groups, "roots": rows, "features": features,
            "sanity": checks,
            "undefined_symbols": dict(image.undefined),
        }, fh, indent=1)
    print("\n%s" % os.path.relpath(args.json, SIM))


if __name__ == "__main__":
    main()
