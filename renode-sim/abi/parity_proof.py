#!/usr/bin/env python3
"""Prove the functions boundary.yaml's parity_exceptions lists act like stock's.

lib_parity.py compares instructions, so it cannot accept a function GCC ordered
differently. This runs both copies symbolically from the same registers, with
every read of RAM or a peripheral returning a value shared by position, and
requires the same memory accesses and calls in the same order, the same values,
and the same callee-saved registers on the way out. A control pair with a known
difference has to be reported, or the comparison itself proves nothing.

relink_startup is not here: it has no stock counterpart. angr is a heavy
dependency and the result only moves when the toolchain or the source does, so
relink.sh does not run this. Needs a Python with angr:

    python3 abi/parity_proof.py
"""

import logging
import os
import subprocess
import sys

import angr
from angr import claripy

for noisy in ("angr", "pyvex", "cle"):
    logging.getLogger(noisy).setLevel("ERROR")

ABI = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(ABI)
NM = os.path.expanduser(
    "~/ref-build/tc/arm-gnu-toolchain-13.2.Rel1-x86_64-arm-none-eabi/bin/arm-none-eabi-nm"
)
STOCK_IMAGE = os.path.join(SIM, "flash.bin")
RELINKED_IMAGE = os.path.join(SIM, "out", "flash-relinked.bin")
RELINKED_ELF = os.path.join(SIM, "out", "relink", "relinked.elf")

STACK = 0x20010000
STACK_BELOW = 0x200
STACK_ABOVE = 0x100
FLASH_END = 0x100000
RETURN = 0x7ffffff0
MAX_STEPS = 400
ENTRY_REGISTERS = ("r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11", "r12")
CALLEE_SAVED = ("r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11", "sp")

STOCK_X_PORT_START = 0x74078
STOCK_X_PORT_CALLS = {"setup": 0x74034, "vfp": 0x73dfc, "first": 0x73dd0, "switch": 0x7333c}
STOCK_X_PORT_NEVER_RETURNS = 0x740c8
RELINKED_X_PORT_NEVER_RETURNS_OFFSET = 0x50
STOCK_CRITICAL_NESTING = 0x20007208

STOCK_SAADC = 0x7dc2c
STOCK_SAADC_LOOP = 0x7dc38
STOCK_SAADC_CLEAR = 0xa4e3a
STOCK_SAADC_CONTROL_BLOCK = 0x20021a00
RELINKED_SAADC_LOOP_OFFSET = 0xc
# The loop's bound is the channel count, which is symbolic here, so the proof is
# of one iteration; each iteration starts from the same state shape.
ONE_MORE_LOOP_HEAD = 1

SHARED_ENTRY = {name: claripy.BVS(name, 32, explicit_name=True) for name in ENTRY_REGISTERS}


def relinked_symbols():
    listing = subprocess.check_output([NM, RELINKED_ELF], text=True)
    symbols = {}
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) == 3:
            symbols.setdefault(fields[2], int(fields[0], 16) & ~1)
    return symbols


def is_ast(value):
    return isinstance(value, claripy.ast.Base)


def explore(image, start, calls, same_data, stops, loop_heads_allowed=0):
    """Every path from `start`, each carrying its ordered events.

    `same_data` maps this image's data addresses to a name for what they hold, so
    the same variable at different addresses compares equal.
    """
    project = angr.Project(
        image,
        main_opts={"backend": "blob", "arch": "armel", "base_addr": 0, "entry_point": start | 1},
        auto_load_libs=False,
    )
    state = project.factory.blank_state(
        addr=start | 1,
        add_options={
            angr.options.SYMBOL_FILL_UNCONSTRAINED_MEMORY,
            angr.options.SYMBOL_FILL_UNCONSTRAINED_REGISTERS,
        },
    )
    for name, value in SHARED_ENTRY.items():
        setattr(state.regs, name, value)
    state.regs.lr = RETURN | 1
    state.regs.sp = STACK
    state.globals["events"] = ()
    state.globals["reads"] = 0
    state.globals["loop_heads"] = 0

    def named(s, address):
        if s.solver.symbolic(address):
            return address
        concrete = s.solver.eval(address)
        return same_data.get(concrete, concrete)

    def on_stack(s, address):
        if s.solver.symbolic(address):
            return False
        return STACK - STACK_BELOW <= s.solver.eval(address) < STACK + STACK_ABOVE

    def in_flash(s, address):
        return not s.solver.symbolic(address) and s.solver.eval(address) < FLASH_END

    def on_read(s):
        address = s.inspect.mem_read_address
        if on_stack(s, address) or in_flash(s, address):
            return
        index = s.globals["reads"]
        s.globals["reads"] = index + 1
        s.inspect.mem_read_expr = claripy.BVS(
            f"read{index}", s.inspect.mem_read_length * 8, explicit_name=True
        )
        s.globals["events"] += (("read", named(s, address), s.inspect.mem_read_length),)

    def on_write(s):
        address = s.inspect.mem_write_address
        if on_stack(s, address):
            return
        s.globals["events"] += (
            ("write", named(s, address), s.inspect.mem_write_length, s.inspect.mem_write_expr),
        )

    state.inspect.b("mem_read", when=angr.BP_AFTER, action=on_read)
    state.inspect.b("mem_write", when=angr.BP_BEFORE, action=on_write)

    class Call(angr.SimProcedure):
        def run(self, name=None):
            s = self.state
            s.globals["events"] += (("call", name, s.regs.r0),)
            s.regs.r0 = claripy.BVS("returned_" + name, 32, explicit_name=True)
            self.jump(s.regs.lr)

    for name, address in calls.items():
        project.hook(address | 1, Call(name=name))

    manager = project.factory.simgr(state)
    finished = []
    for _ in range(MAX_STEPS):
        running = []
        for s in manager.active:
            if s.solver.symbolic(s.regs.pc) or s.addr & ~1 == RETURN & ~1:
                finished.append(s)
                continue
            if s.addr & ~1 in stops:
                if s.globals["loop_heads"] >= loop_heads_allowed:
                    finished.append(s)
                    continue
                s.globals["loop_heads"] += 1
            running.append(s)
        manager.stashes["active"] = running
        if not manager.active:
            break
        manager.step()
        for s in manager.unconstrained:
            # An indirect call through the control block, the saadc user's handler.
            s.globals["events"] += (
                ("callback", s.regs.pc, s.regs.r0, s.memory.load(s.regs.sp + 4, 1),
                 s.memory.load(s.regs.sp + 8, 1), s.memory.load(s.regs.sp + 9, 1)),
            )
            finished.append(s)
        manager.stashes["unconstrained"] = []
        if manager.errored:
            sys.exit(f"angr failed on a path: {manager.errored[0]}")
    if manager.active:
        sys.exit(f"still running after {MAX_STEPS} steps at {hex(manager.active[0].addr)}")
    return finished


def event_difference(ours, theirs, constraints, solver):
    if len(ours) != len(theirs):
        return f"{len(ours)} events against {len(theirs)}"
    for index, (left, right) in enumerate(zip(ours, theirs)):
        if left[0] != right[0]:
            return f"event {index}: {left[0]} against {right[0]}"
        for x, y in zip(left[1:], right[1:]):
            if not is_ast(x) and not is_ast(y):
                if x != y:
                    return f"event {index}: {x} against {y}"
                continue
            size = x.size() if is_ast(x) else y.size()
            lhs = x if is_ast(x) else claripy.BVV(x, size)
            rhs = y if is_ast(y) else claripy.BVV(y, size)
            if lhs.size() != rhs.size():
                return f"event {index} {left[0]}: {lhs.size()} bits against {rhs.size()}"
            if solver.satisfiable(extra_constraints=constraints + [lhs != rhs]):
                return f"event {index} {left[0]}: {x} against {y}"
    return None


def differences(stock_paths, relinked_paths):
    """Every overlapping pair of paths must agree, and stock's inputs must all be covered."""
    solver = claripy.Solver()
    found = []
    for stock in stock_paths:
        stock_constraints = list(stock.solver.constraints)
        covered = []
        for relinked in relinked_paths:
            relinked_constraints = list(relinked.solver.constraints)
            both = stock_constraints + relinked_constraints
            if not solver.satisfiable(extra_constraints=both):
                continue
            covered.append(claripy.And(*relinked_constraints) if relinked_constraints else claripy.true())
            difference = event_difference(stock.globals["events"], relinked.globals["events"], both, solver)
            if difference:
                found.append(difference)
            if stock.solver.symbolic(stock.regs.pc) or relinked.solver.symbolic(relinked.regs.pc):
                continue
            if stock.addr & ~1 != RETURN & ~1 or relinked.addr & ~1 != RETURN & ~1:
                continue
            for name in CALLEE_SAVED:
                unequal = getattr(stock.regs, name) != getattr(relinked.regs, name)
                if solver.satisfiable(extra_constraints=both + [unequal]):
                    found.append(f"{name} differs on return")
        if not covered or solver.satisfiable(
            extra_constraints=stock_constraints + [claripy.Not(claripy.Or(*covered))]
        ):
            found.append("a stock path has no relinked path")
    return found


def main():
    symbols = relinked_symbols()
    x_port = symbols["xPortStartScheduler"]
    x_port_calls = {
        "setup": symbols["vPortSetupTimerInterrupt"],
        "vfp": symbols["vPortEnableVFP"],
        "first": symbols["vPortStartFirstTask"],
        "switch": symbols["vTaskSwitchContext"],
    }
    stock_x_port = lambda: explore(
        STOCK_IMAGE, STOCK_X_PORT_START, STOCK_X_PORT_CALLS,
        {STOCK_CRITICAL_NESTING: "uxCriticalNesting"}, {STOCK_X_PORT_NEVER_RETURNS},
    )
    relinked_x_port = lambda same_data: explore(
        RELINKED_IMAGE, x_port, x_port_calls, same_data,
        {x_port + RELINKED_X_PORT_NEVER_RETURNS_OFFSET},
    )
    saadc = symbols["saadc_event_limits_handle"]
    results = {
        "xPortStartScheduler": differences(
            stock_x_port(), relinked_x_port({symbols["uxCriticalNesting"]: "uxCriticalNesting"})
        ),
        "saadc_event_limits_handle": differences(
            explore(
                STOCK_IMAGE, STOCK_SAADC, {"clear": STOCK_SAADC_CLEAR},
                {STOCK_SAADC_CONTROL_BLOCK: "m_cb"}, {STOCK_SAADC_LOOP}, ONE_MORE_LOOP_HEAD,
            ),
            explore(
                RELINKED_IMAGE, saadc, {"clear": symbols["nrf_saadc_event_clear.constprop.0"]},
                {symbols["m_cb"]: "m_cb"}, {saadc + RELINKED_SAADC_LOOP_OFFSET}, ONE_MORE_LOOP_HEAD,
            ),
        ),
    }
    # The same pair with one variable left unnamed writes a different address.
    control = differences(stock_x_port(), relinked_x_port({}))
    for name, found in results.items():
        print(name, "equivalent" if not found else found[:6])
    print("control", "reported" if control else "NOT REPORTED")
    if not control or any(results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
