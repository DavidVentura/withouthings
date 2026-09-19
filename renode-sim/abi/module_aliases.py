#!/usr/bin/env python3
"""Log tags that are two spellings of one module.

    python3 abi/module_aliases.py            # what the image establishes, as a report

The module partition is read off the `[TAG]` every log line opens with, and a
module that logs under two tags is two modules to it. That is not a nuisance in
the partition alone: abi/autonames.py's `bymodule` rule refuses a body whose
callees and globals put it in one module while the tags put it in another, and
a good part of those refusals are the same module disagreeing with itself.

Two readings say a pair of tags is one module.

  co-logged       one function logs lines under both tags. A function on a
                  boundary logs its caller's tag and its own, so a single such
                  function is not enough on its own.
  shared context  a static RAM word that the functions directly tagged with
                  either tag reach and no third tagged module reaches. Module
                  state is private by construction, so two tags reaching the
                  same private word are one module's code.

A pair is accepted on two shared static words, or on one shared word plus at
least one co-logging function; a pair with neither stays two modules and the
`bymodule` refusal at its bodies stands. Acceptance then feeds back: once
INITDBLIB is DBLIB, a word DBLIB, INITDBLIB and DBLIB_PORT all reach has two
tagged modules touching it rather than three, so DBLIB_PORT joins on the next
round. The rounds stop when one adds nothing.

The guard is what keeps the feedback from collapsing the partition. Accepted
pairs are merged transitively, and a group of more than two tags is only a
module if one of its tags is a substring of every other -- DBLIB, INITDBLIB and
DBLIB_PORT are one name written three ways, while AUTO_BURST sharing a word
with SPO2 and another with PPG_AFIB would make SPO2 and PPG_AFIB one module,
which they are not. A group that fails the guard is refused whole and every
tag in it stays on its own.

The answer is recorded in abi/facts.yaml under `module_aliases`, which is what
abi/autonames.py reads: this file is the measurement that establishes the
entries there, and re-running it is how they are checked.
"""

import collections
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))

RAM_LO, RAM_HI = 0x20000000, 0x20040000


# The parse of abi/facts.yaml's alias groups, by the file it came from. The
# tag of every log line in the image goes through `load`, so re-reading the
# file per line would cost more than the whole partition; the file is a static
# fact of the repo rather than anything a run wires up, so one read of it
# answers every call.
_BY_PATH = {}


def load(path=None):
    """{tag: the tag the partition uses for it}, from abi/facts.yaml.

    Only the groups the facts file records, and only downwards: a tag with no
    alias maps to itself, which is what makes this safe to apply to every tag
    the log lines carry.
    """
    path = path or os.path.join(HERE, "facts.yaml")
    if path in _BY_PATH:
        return _BY_PATH[path]
    with open(path) as fh:
        doc = yaml.safe_load(fh)
    out = {}
    for group in doc.get("module_aliases") or []:
        for tag in group["tags"]:
            if tag in out and out[tag] != group["module"]:
                sys.exit("abi/facts.yaml: %s is aliased to both %s and %s"
                         % (tag, out[tag], group["module"]))
            out[tag] = group["module"]
    _BY_PATH[path] = out
    return out


def _stem(tags):
    """The tag of a group that every other tag of it spells out, or None."""
    for tag in tags:
        if all(tag in other for other in tags):
            return tag
    return None


def _components(pairs):
    """The transitive closure of a set of accepted pairs, as {tag: [tags]}."""
    parent = {}

    def find(tag):
        parent.setdefault(tag, tag)
        while parent[tag] != tag:
            parent[tag] = parent[parent[tag]]
            tag = parent[tag]
        return tag

    for a, b in pairs:
        parent[find(a)] = find(b)
    groups = collections.defaultdict(list)
    for tag in parent:
        groups[find(tag)].append(tag)
    return [sorted(g) for g in groups.values()]


def _canonical(pairs):
    """{tag: the group's own spelling} for every accepted pair."""
    out = {}
    for group in _components(pairs):
        head = _stem(group) or sorted(group, key=lambda t: (len(t), t))[0]
        for tag in group:
            out[tag] = head
    return out


def derive(img, ex, sites, log_tag, rounds=8):
    """-> ({(a, b): (co-logging functions, shared words)}, refused groups).

    One round reads the two signals over the tags as the rounds so far
    canonicalise them, accepts the pairs that clear the bar and the guard, and
    the next round reads them again with those merges in place.
    """
    tagged = [(s["fn"], log_tag(s["fmt"])) for s in sites
              if s["fn"] is not None and log_tag(s["fmt"])]
    accepted, refused, canon = {}, {}, {}
    for _ in range(rounds):
        per_fn = collections.defaultdict(collections.Counter)
        for fn, tag in tagged:
            per_fn[fn][canon.get(tag, tag)] += 1
        co = collections.defaultdict(set)
        direct = {}
        for fn, counts in per_fn.items():
            top = counts.most_common()
            # The same majority the partition itself takes a function's module
            # by: a function on a boundary claims no static context.
            if len(top) == 1 or top[0][1] > top[1][1]:
                direct[fn] = top[0][0]
            seen = sorted(counts)
            for i, a in enumerate(seen):
                for b in seen[i + 1:]:
                    co[(a, b)].add(fn)
        reaching = collections.defaultdict(set)
        for fn, tag in direct.items():
            for target in ex.pool_targets.get(fn, ()):
                word = img.word(target)
                if word is not None and RAM_LO <= word < RAM_HI:
                    reaching[word].add(tag)
        shared = collections.defaultdict(list)
        for word, tags in reaching.items():
            if len(tags) == 2:
                shared[tuple(sorted(tags))].append(word)
        candidates = {}
        for pair in set(co) | set(shared):
            fns, words = sorted(co.get(pair, ())), sorted(shared.get(pair, ()))
            if len(words) >= 2 or (words and fns):
                candidates[pair] = (fns, words)
        blocked = set()
        for group in _components(list(candidates) + list(accepted)):
            if len(group) > 2 and _stem(group) is None:
                refused["|".join(group)] = group
                blocked |= set(group)
        added = 0
        for pair, evidence in sorted(candidates.items()):
            if pair in accepted or set(pair) & blocked:
                continue
            accepted[pair] = evidence
            added += 1
        canon = _canonical(accepted)
        if not added:
            break
    return accepted, refused


def main():
    sys.path.insert(0, HERE)
    import autonames  # the image, the disassembly, the partition and log_tag

    img = autonames.Image(os.path.join(autonames.SIM, "appl.bin"),
                          os.path.join(autonames.SIM, "out", "appl.dis"))
    want = set(autonames.DEFAULT_CLASSES.split(","))
    export = os.path.join(HERE, "out", "ghidra")
    ex = autonames.Export(export, autonames.prior_addresses(export, want))
    every = autonames.wlog_sites(img, ex)
    sites = [s for s in every if s["fmt"] is not None]
    sites += autonames.widen_sites(img, ex, every, autonames.wlog_arg_words(img, ex),
                                   autonames.pool_loaders(ex))
    # The raw tag, not the canonical one: this is the measurement that decides
    # what the aliases are, so it may not read the answer it is deriving.
    accepted, refused = derive(img, ex, sites, autonames.raw_log_tag)
    print("%d tag pairs are one module, %d groups refused by the guard"
          % (len(accepted), len(refused)))
    canonical = _canonical(list(accepted))
    for group in _components(list(accepted)):
        print("  %-14s %s" % (canonical[group[0]], ", ".join(group)))
    for pair, (fns, words) in sorted(accepted.items()):
        print("    %-28s %s%s"
              % ("/".join(pair),
                 "co-logged by %s; " % ", ".join("0x%x" % f for f in fns) if fns else "",
                 "shares %s" % ", ".join("0x%x" % w for w in words) if words else ""))
    for group in refused.values():
        print("  refused: %s -- no tag of the group spells out the rest"
              % ", ".join(group))
    held = load()
    now = _canonical(list(accepted))
    if held != now:
        print("abi/facts.yaml module_aliases disagrees with this run:")
        for tag in sorted(set(held) | set(now)):
            if held.get(tag) != now.get(tag):
                print("  %-22s facts %-14s here %s"
                      % (tag, held.get(tag, "-"), now.get(tag, "-")))
        return sys.exit(1)
    print("abi/facts.yaml module_aliases agrees: %d tags in %d groups"
          % (len(held), len(set(held.values()))))


if __name__ == "__main__":
    main()
