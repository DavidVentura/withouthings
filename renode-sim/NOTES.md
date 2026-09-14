# Renode sim — working notes

Target: ScanWatch 2 (HWA10), nRF52840 + Nordic S140 v7.3.0 + appl 3411.
Renode v1.17.0 portable (put `renode` on PATH; see README for prereqs).

## Status

- **Phase 0 (boot → wlog): ACHIEVED** via `run-logs.resc` (pragmatic shortcuts,
  see "Fidelity gaps"). Readable boot log:
  ```
  [M] boot
  [M] rtc_value : 0
  [M] HWA10 boot reason = 0
  ```
- **Corrected root cause:** the first wedge was NOT primarily missing interrupts.
  The app calls `sd_mbr_command` (svc #24 @ 0x9301e) very early; under the
  emulated MBR that SVC **HardFaults** (HFSR forced, fault PC 0x93020). The
  "spin" was the fault handler trying to log and blocking on the full log ring.
  Patching that thunk to return success gets real boot logs. (The earlier
  interrupt-forwarding analysis still holds for what's needed *later* — the RTOS
  scheduler — but it was not the first blocker.)
- **Next blocker:** after the 3 boot lines the app clears the OLED-power delay
  loops and stops at another driver-completion wait (0x3b444/0x3b4c0 semaphore
  take on a different object) — whack-a-mole, because those ISRs never run
  without the scheduler/tick. Phase 1/2 need that resolved.
- **Phase 1 (framebuffer) / Phase 2 (crown): not started** — both depend on a
  booting sim. Pre-traced RE for them is in the scratchpad `peripheral_findings.md`
  (crown I2C IDs 0x31/0x92 at addr 0x79, OLED on SPIM, pins) — apply once booting.

## What works

- `extract.py` rebuilds `appl.bin`/`bl.bin`/`sd.bin` and assembles `flash.bin`
  (1 MiB: MBR+SD @0x0, appl @0x27000, bl @0xfc000).
- Platform `hwa10.repl` = the stock Renode nrf52840 platform with the SVD pointed
  at the local `NRF52840.svd` (no per-run network download).
- `boot.resc` loads the flash and boots from the MBR vector table at 0x0.
- Boot chain executes: the CPU runs MBR → SD → appl and reaches application code
  (PC lands in the 0x27000–0xf1000 range), ~20M instructions in 0.2 s virtual.

## The blocker (diagnosed)

The CPU wedges spinning at **0x3b4cc**, a FreeRTOS-style wait:

- 0x3b4c0 is a counting-semaphore / queue take: it polls `[obj+0x11c]` for
  nonzero inside a critical section (`0x73e3c` = `msr BASEPRI,#0xc0`; `0x73e5c`
  restores it). It never becomes nonzero.
- Immediate caller is **0x56e8e**, a putchar-to-ring-buffer routine: it writes a
  byte, and when the ring is full it waits on semaphore object **0xb2588**
  (`0x56ec8 → 0x9c238 → 0x3b4c0`). So a **log/console producer blocks because its
  consumer never drains the ring**.
- The consumer never runs because **no NVIC interrupt is ever enabled**. After
  boot: `ISER0 = ISER1 = 0` (nothing enabled), yet **`ISPR0` bit 2 is set —
  UARTE0 (IRQ 2) is pending**. SysTick off, RTC1 not counting. So the UARTE0
  TX-complete ISR that would drain the ring never fires, and there is no RTOS
  tick to schedule anything.
- Interrupts are never enabled because the application enables them through the
  SoftDevice: it issues SVCs (SVC handler is the MBR trampoline at **0xAA4**;
  first observed call is **SVC #24 from PC 0x93020**), and the S140's SVC-based
  NVIC virtualization does not reflect into Renode's NVIC. Running the real
  SoftDevice under Renode does not bring up its interrupt management — the known
  hard part of emulating a SoftDevice image.

Confirmation experiment: force-writing `ISER0/1 = 0xFFFFFFFF` and clearing
BASEPRI/PRIMASK after boot **does** kick the CPU out of the spin (it starts
servicing ISRs), but enabling *all* IRQs causes an interrupt storm (peripheral
models re-asserting), so 0.1 s of virtual time never completes. So interrupts are
the correct lever; the enable must be *selective*, matching what the firmware
actually requested.

## Next step — emulate the SoftDevice NVIC/SVC surface

Do NOT try to run the real S140 for interrupts. Instead intercept the SD SVCs
with a Renode CPU hook at the MBR SVC trampoline (0xAA4) and emulate the handful
the app needs, so the request reaches Renode's NVIC:

- `sd_nvic_EnableIRQ(irq, prio)` → `NVIC ISER` set that bit + set priority.
- `sd_nvic_DisableIRQ` / `SetPriority` / `ClearPendingIRQ` / `SetPendingIRQ`.
- `sd_softdevice_enable` / `sd_softdevice_vector_table_base_set` → return success.
- `sd_app_evt_wait` → WFE / return (idle).
- Clock/rand SVCs as they surface (`sd_clock_hfclk_request`, `sd_rand_*`).

Enumerate the full SVC set by hooking 0xAA4 and decoding the SVC immediate
(`byte at stacked_PC-2`, stacked PC at `SP+0x18`; pick MSP vs PSP via
EXC_RETURN bit 2). Map SVC numbers to S140 7.3.0 `nrf_svc.h` / `nrf_nvic.h`.
The RTOS tick source (SysTick vs an RTC) must then actually run so the scheduler
preempts — verify RTC1/SysTick get programmed once IRQs are live.

Alternative if SVC stubbing proves fiddly: binary-patch the app's
`sd_nvic_*`/`sd_app_evt_wait` thunks to direct CortexM operations (write NVIC,
`wfe`) so the SoftDevice is bypassed for interrupt control entirely.

## Fidelity gaps / hacks (what the sim does NOT faithfully exercise)

Every shortcut currently applied, and what it means we are not testing:

1. **`sd_mbr_command` patched to return success** (`svc #24 @0x9301e` → `movs r0,#0`).
   Real hardware runs the MBR command (IRQ-forward-address / vector-table-base
   setup, SD/BL copy). The sim does none of that — so **SoftDevice interrupt
   forwarding and any MBR-mediated flash op are not exercised**, and anything that
   depends on the MBR having actually set the forward address will behave
   differently. This is why interrupts must be handled separately (VTOR bypass).

2. **Log-ring semaphore 0xb2588 forced non-blocking.** The real drain is a
   consumer (UARTE0 DMA ISR or a flush task) emptying the ring. Forcing the take
   to succeed means **the real UARTE ISR / log-flush path is not tested**; we read
   the bytes by hooking the producer putchar instead. Log *content* is real; the
   *transport* is bypassed.

3. **wlog read by hooking the ring putchar (0x56e8e)**, not by decoding the
   on-device binary wlog records or reading real UART TX. Faithful to the text
   being logged, but not to how wlog is stored/transported.

4. **CPU busy-delay loops are just waited out** (raised `PerformanceInMips`).
   Not a correctness gap, but sim wall-clock ≠ device timing; any timing-sensitive
   handshake that relies on those delays matching real peripheral latency is not
   validated.

Planned/experimental hacks NOT in `run-logs.resc` but used while probing (each a
gap if adopted): `VectorTableOffset 0x27000` (bypasses SD interrupt forwarding —
app handles its own IRQs); hand-writing NVIC `ISER`; clearing `BASEPRI`/`PRIMASK`;
force-starting RTC1; forcing *all* semaphore takes non-blocking (broke boot —
rejected). Record here before adopting any.

## Interrupt bring-up (what was tried; where it stands)

SVC #24 @ 0x93020 is **`sd_mbr_command`** (MBR SVC base 0x18=24) — the app sets up
SoftDevice interrupt forwarding, which the emulated MBR/SD does not honour, so
interrupts never reach the app's handlers. The app's own vector table at 0x27000
has real handlers: IRQ2 UART0→0x3b3ed, IRQ3/4 TWI→0x3abf5/0x3ac05, IRQ6 GPIOTE→
0x39fb9, IRQ9/10 TIMER1/2→0x3af6d/0x3af81, **IRQ17 RTC1→0x3a6f1 (the tickless
FreeRTOS tick — SysTick is the default stub 0x349d9)**. SD-owned IRQs (0 CLOCK,
1 RADIO, 8 TIMER0, 11 RTC0) point at the default stub.

Approach that works to route interrupts: **`cpu VectorTableOffset 0x27000`** after
boot — the app then handles its own IRQs directly, no SD forwarding needed. This
sidesteps sd_mbr_command entirely and is simpler than emulating the SVC surface.

Confirmed interrupts are the lever: with VTOR=0x27000, enabling NVIC IRQs and
**clearing BASEPRI/PRIMASK** kicks the CPU out of the 0x3b4cc spin (the spinning
context holds a raised BASEPRI that masks the giving ISR). Without clearing
BASEPRI the enabled+pending IRQ stays masked and the spin continues.

Remaining obstacle: **enabling the app IRQs storms.** The moment IRQs are enabled
with BASEPRI clear, an ISR fires, returns, and immediately re-fires — 0.002 s of
virtual time does not complete. Cause is a Renode peripheral-model / event-clear
mismatch: the app's ISR does not see the event as cleared the way the real
peripheral would, so it re-pends. This must be resolved per source. Enabling only
IRQ2 (UART) *without* clearing BASEPRI runs cleanly but does not give the spin's
semaphore (0xb2588), so the giver is a different ISR — trace the give at 0x3b444
(`ldr/str [obj+0x11c]`) to its caller/ISR to find the exact interrupt needed.

Next actions:
- Identify which single IRQ gives semaphore 0xb2588 (find the ISR that calls the
  give 0x3b444 with that object), enable only that one, and fix its Renode model's
  event/clear semantics so it fires once rather than storming.
- The RTOS tick (RTC1, IRQ17) needs LFCLK running and RTC1 counting; verify the
  firmware started RTC1 (it read COUNTER=0), and that the RTC1 model ticks and its
  IRQ clears on the handler's EVENTS_* write-0.
- Keep `cpu VectorTableOffset 0x27000` as the interrupt-forwarding fix; only add
  SVC emulation (route a) if FreeRTOS's own SVCs — vPortSVCHandler at app SVC
  vector 0x27e21, PendSV 0x27e51 — need it (they should work once VTOR=app).

`bringup-experiment.resc` captures the VTOR-bypass + selective-enable harness with
the log hooks, for continuing this.

## Renode gotchas learned

- CPU hooks run IronPython (py2 `print`). `cpu.GetRegisterUnsafe(n).RawValue`
  reads regs; get the bus via `cpu.GetMachine().SystemBus` (the bare `sysbus`
  name is not always in a hook's scope, e.g. in exception context).
- Globals set inside a hook persist across hook calls but are a **different**
  namespace from the monitor's `python` command — print results from inside the
  hook, don't read them back via a separate `python` line.
- Hooks only fire once the target PC is actually reached; short `RunFor` windows
  (this firmware reaches the spin at ~50 ms virtual) will look like "never fired".
- wlog is not text-over-UART at the call site: high-level loggers (0x8f494,
  0x8f460, 0x50ca8 [fmt in r2], 0x9b1c8, 0x5e7a8 [fmt in r0]) format into a ring
  that a UARTE0-DMA consumer drains. Hooking those loggers and reading the fmt
  pointer gives readable logs once the CPU gets past the wedge.
