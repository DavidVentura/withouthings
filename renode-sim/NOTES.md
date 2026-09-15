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
- **Scheduler bring-up: PARTIAL.** The FreeRTOS scheduler runs (PendSV switches
  tasks). A task reaches the **OLED display init** and stalls in a power-on delay
  that reads RTC1 time — RTC1 is never started, and the tick doesn't self-sustain.
  Screen (Phase 1) not reached. See "Scheduler bring-up" and "Coverage progress
  bar".
- **Coverage progress bar: DONE** (`trace.resc` + `coverage.py` + `symbols.txt`) —
  native trace, maps the boot path and the exact stall.
- **FLASH INIT PASSES — boot reaches the OLED init.** spi2 = external MX25R flash;
  `NrfSpim.cs` is a real SPI-NOR flash model backed by the 8 MB dump. The blocker
  was a model bug (SPIM TXD/RXD.AMOUNT not updated -> driver saw 0 bytes moved and
  re-sent 0x66 forever); setting AMOUNT fixed it. Boot now clears the full flash
  sequence (reset/RDID/RDSR/WREN/config reads/READ) and advances to the OLED init
  (`oled_delay_poll`), idling on the tick. See "*** FLASH INIT PASSES ***".
  Remaining: reaching the display SPI is perf-limited — the `wfe`→nop tick
  busy-poll makes long runs slow; 2 s virtual reaches only the OLED GPIO/power-on +
  settle-delay phase (still idling), not the display transfers, so the OLED bus is
  NOT yet identified. A SEVONPEND-honoring WFE (Renode core) is the real perf fix.
- **Tick self-sustains + SPLASH-SCREEN MILESTONE reached.** With `wfe`→nop
  (Renode doesn't honour the firmware's SEVONPEND) the FreeRTOS tick recurs on
  its own and boot advances to the **OLED display init driving SPIM2 traffic**
  (pin-select, frequency, EasyDMA transfers). Stalls at the SPIM END event, which
  Renode's SPI model doesn't raise. See "Tick self-sustain + reaching the OLED
  SPIM". This is the display-init-running milestone.
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

## Scheduler bring-up (partial — where it stands)

Once `sd_mbr_command` is patched and the log semaphore (0xb2588) is unblocked in
BOTH take primitives (0x3b4c0 AND 0x3b444 — the log ring uses both), the delay
loops clear and boot runs into the **FreeRTOS tickless idle** at **0x7400c**:
`wfe`, then poll NVIC ISPR for any pending interrupt, sleep again if none. So the
scheduler is already up; it is idle waiting for the tick.

- **Tick source is RTC2 (0x40024000), IRQ36 — not RTC1.** (The RTC1→0x3a6f1
  vector is something else.) RTC2 counts (LFCLK is running, PRESCALER=0x20 →
  ~1 kHz), and its COMPARE[0] interrupt is enabled at the peripheral
  (RTC2.INTENSET bit16). The compare fired (EVENTS_COMPARE0=1, IRQ36 pending in
  NVIC ISPR1). App RTC2 handler = 0x73e71.
- **Why it never woke:** IRQ36 was never enabled in the NVIC (ISER1=0 — the
  `sd_nvic_EnableIRQ` path didn't take effect), and Renode's `wfe` does not
  re-evaluate the wake when the enable/pending changes after the fact.
- **Getting one tick:** VTOR→0x27000 (app handles IRQs), NVIC-enable IRQ36, and
  either set SEVONPEND or patch the `wfe` (0x7400a `0x463a`→ nop 0xBF00) so the
  idle busy-polls. Then the RTC2 ISR (0x73e71) runs and a **PendSV context switch
  (0x27e51) fires** — so tick + switch both work.
- **Not self-sustaining:** after the tick the idle does not reprogram RTC2 CC0
  (it stays 0x3A while COUNTER runs past it), so no second compare. Injecting a
  steady tick (periodically pending IRQ36) keeps the scheduler ticking but
  produces **no new logs** — the init/GUI tasks are blocked on *peripheral driver
  completions* (TWI/SPI/GPIO ISRs), not on tick delays.
- **Next layer (to reach the GUI):** bring up the specific peripheral ISRs the
  blocked tasks wait on, with correct Renode event/clear semantics so they fire
  once and don't storm. Find each blocked task's wait object and the ISR that
  gives it (trace the give paths; the give increments `[obj+0x11c]`), and enable
  only those sources. This is the storm-prone per-peripheral work and is the
  remaining effort for Phase 1.

## Coverage progress bar (TASK 1 — done)

`trace.resc` captures a native Renode PC trace (fast; not per-instruction Python
hooks) to `/tmp/hwa10_trace.bin.gz`; `coverage.py` maps it against `symbols.txt`
and prints the known functions in first-execution order plus the loop the boot
ends stuck in. Trace format: `b"ReTrace"` + 3 bytes, then 5-byte entries
(LE u32 PC + 1 flag). Coverage, not logs, is the progress signal.

Current canonical clean-boot result: boot runs sd_mbr_command → wlog → the
FreeRTOS scheduler starts (svc_handler → **PendSV context switch**) → a task runs
the **OLED init** (`contrast_task_create`, `oled_gpio_power_cfg`) and ends stuck
in **`oled_delay_poll` → `get_time_rtc1`**, i.e. a power-on delay reading RTC1
time — while the steady state is the tickless idle WFE-halt. So the display init
task is already running; it is blocked on time not advancing.

## Are the hacks masking missing platform init? (answering the review question)

Checked with Renode's native `LogPeripheralAccess` on clock/rtc. Findings:

- **LFCLK / RTC2 tick init is present and works** (not a platform gap). At
  `rtc2_tick_init` (0x74034) the firmware does StartLFCLK + RTC2
  PRESCALER=32/INTENSET/CLEAR/START; RTC2 counts and its COMPARE fires. The
  `LFCLKStarted` read there is a write-ordering read-back, not a blocking wait.
- **RTC1 is never started in the reached boot** — the firmware only *reads* RTC1
  COUNTER (via `get_time_rtc1`), never writes its TASKS_START, so `get_time`
  returns 0 forever and the OLED delay never elapses. This is a firmware-sequence
  effect (RTC1's init is downstream of / gated by the parts that don't run),
  **not** a missing pull-up/clock in the platform: force-writing RTC1 TASKS_START
  makes Renode's RTC1 model count fine.
- **The genuine emulator-model gaps** are: (a) Renode's `wfe` does not re-wake
  when an interrupt becomes pending/enabled after the fact (so the tickless idle
  never wakes on the RTC2 compare — worked around with SEVONPEND or `wfe`→nop);
  (b) the RTC2 compare is not re-armed for the *next* tick under these conditions
  (CC0 stays stale), so ticks don't self-sustain. These are the model-level fixes
  worth doing instead of the injected-tick hack.

So: the review instinct was right to check — one real requirement (RTC1 must be
running) was being papered over, and two items are true Renode-model gaps (WFE
wake, RTC compare re-arm). None so far is a missing GPIO/clock in the platform
description. The principled path for Phase 1: get RTC1 running the way the
firmware would (find/trigger its init) and fix the RTC2/WFE model behaviour so
the tick self-sustains — then the OLED delay elapses and SPIM traffic should
appear.

## Tick self-sustain + reaching the OLED SPIM (this round)

Traced the tick mechanism with `LogPeripheralAccess` (native, not hooks):

- The FreeRTOS tick is **RTC2 COMPARE0** (not TICK). The tickless idle computes
  `CC0 = counter + delay` and writes it (0x73f40) — the compare IS reprogrammed
  and Renode's RTC2 fires it correctly. Earlier "CC0 stuck at 0x3A" was because
  the idle wake-path never ran (below), not because the model ignores CC0.
- **The idle is a polled pattern**: `sev; wfe; poll NVIC ISPR`. It expects the
  RTC2 compare to *pend* (IRQ36) and WFE to wake on it via **SEVONPEND**, then it
  reads ISPR, processes the tick inline and reprograms CC0. **Renode's `wfe` does
  not honour SEVONPEND** (doesn't wake on a pending-but-disabled interrupt) — the
  genuine emulator gap. Workaround: patch `wfe`(0x7400a)→nop so the idle
  busy-polls ISPR; it then processes each tick and **self-sustains** (no injected
  ticks — that hack is retired). The patch must be applied at boot (before the
  block is translated) or Renode's block cache keeps the old `wfe`.
- **`VectorTableOffset 0x27000` is principled, not a hack**: the only SD SVCs the
  boot makes are `sd_softdevice_vector_table_base_set(0x27000)` (svc #19) and
  FreeRTOS's start-first-task (svc #0). The former tells the SoftDevice to forward
  interrupts to the app table at 0x27000 — which VTOR=app does directly.

With `wfe`→nop the tick self-sustains and boot advances well past the log ring:
scheduler → OLED init task → **it drives SPIM2 (0x40023000): PinSelect SCK/MOSI/
MISO, Frequency, Config, Enable, then EasyDMA TxDataPointer/Count + TasksStart.**
That is the **splash-screen milestone — display init runs and SPIM traffic
appears.** It then stalls at `spim_wait_end` (0x3aee2) spinning on the SPIM
`EventsEnd` (0x118): **Renode's `NRF52840_SPI` model does not raise the END event
for the EasyDMA transfer**, so the transfer never "completes". Making that model
fire END (a real platform/model fix, like the WFE one) would let the OLED init
proceed through its command sequence — which would then reveal the controller,
resolution and pixel format from the SPIM byte stream.

RTC1 (the busy-delay timebase for `get_time_rtc1`) is also never started by the
reached boot; force-writing its TASKS_START makes it count. Whether the firmware
starts it later (post-SPIM-init) is untraced.

## Rendering a frame — where it stands (no frame yet)

Goal: complete OLED SPIM transfers, capture the command/pixel stream, render a PNG.

What was found by capturing every SPIM transfer (hook at `spim_xfer_start`
0x3ae3e reading TxDataPointer=r5, count=r2, RxDataPointer=r6):

- The OLED driver's **first** action is a **1-byte poll**: send `0x66`, read one
  byte into `oled_rx_buf` (0x2002527e) — and it repeats this **forever** (41,530
  identical transfers, same GPIO state). It is polling the controller and never
  gets the answer it wants, because **Renode's `NRF52840_SPI` has no OLED slave
  to respond** — reads return 0/garbage. So init never advances to sending the
  display-setup commands or pixel data. There is nothing to render yet.
- The controller is **not identifiable from the stream so far**: no controller
  string in the image (driver is abstracted), and 0x66 alone doesn't pin a part.
  Its identity/resolution/format live in the init commands that only get sent
  *after* this poll succeeds.
- Unblocking it needs the poll's expected response. I could not determine it
  cheaply (the check wasn't located; 0x66 is too common to grep). An attempt to
  inject an **echo** of the sent byte into the Rx buffer was **inconclusive** —
  the run didn't finish (the `wfe`→nop busy-poll makes 0.3 s of virtual time take
  >170 s wall, so traced experiments here time out).

Conclusion (honest): the milestone (display init running, real SPIM traffic) is
reached, but a **rendered frame is blocked on modeling the OLED controller as an
SPI slave** that answers the driver's poll — the coordinator's preferred
"custom peripheral". Two concrete follow-ups: (1) trace the check on
`oled_rx_buf` (0x2002527e) to learn the expected poll response, then feed it via
a small SPI-slave model on spi2; (2) that model should also raise `EVENTS_END`
per transfer (the other SPI gap) so both the busy-poll and END-interrupt transfer
paths complete. Then the init commands + RAM-write reveal controller/resolution/
format and the pixel stream can be accumulated into `out/*.png`. Wall-clock: the
busy-poll idle makes long runs expensive; a real WFE-honoring-SEVONPEND fix or a
tighter run window is worth it before the render pass.

## Custom SPIM controller — END gap FIXED; OLED detect loop remains (this round)

Built a custom SPIM controller `NrfSpim.cs` (`SPI.NrfSpimCapture`) and swapped it
in for `SPI.NRF52840_SPI` at spi2 in `hwa10.repl` (loaded via relative
`include @NrfSpim.cs`). On `TASKS_START` it performs the EasyDMA transfer
(reads the Tx buffer from memory, writes `PollReply` to the Rx buffer), raises
`EVENTS_END`, and records the byte stream + D/C. **This fixes the SPIM END gap**
cleanly (the coordinator's preferred custom-peripheral route) — the stock model
never ran the DMA. Verified: END fires, the transfer loop completes, and the
full SPIM byte stream is captured (`spi2 DumpStream @<file>`), 1.9M bytes/run.

Test result (honest): the stock model's slave-attach hypothesis was **false**
(attaching an `ISPIPeripheral` slave to `NRF52840_SPI` did not run the DMA or
fire END; the slave's `Transmit` was never called). The custom controller was
needed.

**But no frame yet.** Past the END gap the firmware enters an **OLED
detect/init loop** (`oled_detect_loop_head` 0x5b98c / `oled_detect_send` 0x5b9f8):
it sends command `0x66` (and `0x99`), yields (`task_yield` 0x9e46c pends PendSV),
and loops — waiting for a detect condition. The captured stream is **entirely
`0x66` commands (D/C=command), no data bytes ever**, so it never reaches the
display-config commands or pixel writes. The response value is not the gate:
`PollReply` = 0x00 / 0xFF / 0x01 / 0x66 (echo) all keep it looping. So the detect
wants either a **multi-byte ID/response** (my model returns a single constant for
every Rx byte) or a **GPIO input** (one of the OLED pins as BUSY/TE — P0IN reads
back P0OUT in the sim, so a real input isn't driven). Pinning that is the next
step: trace `oled_detect_fn` (0x5ba40) / `0x9bdba` to see what it compares, then
have the model return the matching ID sequence (and/or drive the BUSY GPIO). Then
the init commands + RAM-write reveal controller/resolution/format and the pixel
stream can be rendered to `out/*.png`. The custom controller + capture pipeline
is in place for that final pass.

Correction to an earlier note: **0x9e46c is a cooperative yield** (pends PendSV),
not a panic handler — I had misattributed the panic address.

## MAJOR RE-IDENTIFICATION: spi2 is the external FLASH, not the OLED

Pushing on the "OLED detect" revealed it is not the OLED at all. **spi2
(0x40023000) is the external MX25R SPI-NOR flash.** Evidence, decisive:

- The 28-entry "config table" at `0xb427c` is a table of **SPI-flash JEDEC IDs**:
  0xC2 Macronix, 0xEF Winbond, 0xBF SST, 0x20 Micron, 0x9D ISSI, 0xC8 GigaDevice,
  0x1F Atmel — one 4-byte id per 16-byte entry. Entry [20] = `0xC2 28 17` =
  **MX25R6435F**, exactly the 8 MB flash `FIRMWARE.md` names.
- The looping command `0x66` then `0x99` is the flash **Reset-Enable / Reset**
  sequence; `flash_read_jedec_id` (0x9ba84) issues `0x9F` (RDID) and matches the
  table. So the "detect" is flash identification, and the async flag it waits on
  is the flash-init handshake, not an OLED response.

So the prior rounds' "OLED init / detect / splash SPIM traffic" was really the
**external-flash bring-up**. The custom controller (`NrfSpim.cs`) now also answers
`0x9F` with the MX25R id (C2 28 17), but the boot still stalls in the flash
reset/detect handshake (an async flag never set in the sim), and forcing past it
(`0x5ba6c`→`movs r0,#1`) crashes downstream (`PC=0xeffffffe`) — the config path
needs state the real detect sets up.

**Implications for a rendered frame (honest):**
- The **real OLED is a separate interface**, reached only *after* flash init — not
  yet located.
- The boot reads configuration/calibration from the external flash, so it can't
  proceed past flash init without it; skipping it crashes.
- The **splash bitmap and display assets are very likely stored in the external
  flash**, of which the sim has **no content** (flash.bin models only internal
  flash: MBR/SD/appl/bl). Rendering the *actual* splash would need a **dump of the
  watch's external SPI flash** loaded behind spi2.
- Cleanest faithful path now: attach a real SPI-flash model (or extend
  `NrfSpim.cs` into one) backed by an external-flash **image**, so the JEDEC id,
  reset handshake, and content reads all behave — then boot proceeds to the real
  OLED interface and the assets are available to render. Without a flash image,
  the frame is blocked regardless of OLED modelling.

This corrects the target: the blocker to a frame is the **external flash
subsystem + its content**, not an OLED controller model. It's the most important
finding of the render effort and redirects it.

## *** FLASH INIT PASSES — boot reaches the OLED init *** (breakthrough)

The watchpoint diagnostic (write-watch on the flash-detect flag at RAM
**0x2002507e** = `*[flash_ctx(0xb244c)+12]`) showed the flag **is** written
(PC 0x9be2a sets it) — so the detect wasn't the real blocker. The real bug was in
`NrfSpim.cs`: the nRF SPIM updates **TXD.AMOUNT (0x54C)** and **RXD.AMOUNT
(0x53C)** with the bytes actually transferred, and the flash driver polls those
for progress (read at 0x3aeb0). My model left them 0, so the driver thought every
transfer moved 0 bytes and re-issued the same command forever (the 2.4M × `0x66`).

**Fix: set TXD.AMOUNT/RXD.AMOUNT after each transfer.** With that, boot **clears
the entire flash init**: the command histogram now shows the real sequence —
reset (`0x66`/`0x99`), RDID (`0x9F` → C2 28 17), RDSR (`0x05`), WREN (`0x06`),
config reads (`0x15`/`0xB9`/`0x01`), and READs from the dump. Coverage then
advances all the way to the **OLED init** (`oled_delay_poll` 0x371c8, the RTC1
power-on delay) and settles at the tickless idle — no longer stuck on flash.

So: real flash model + the 8 MB dump + the AMOUNT fix = flash subsystem works, and
boot is past it into the display bring-up. Remaining: drive the idle/tick far
enough for the display driver to start and identify the OLED bus (the `wfe`→nop
busy-poll makes long runs slow; a real SEVONPEND-honoring WFE would help).

## Real external-flash model + dump loaded (this round)

The coordinator provided a verified 8 MB dump of the watch's real external SPI
flash. It is copied to `renode-sim/external_flash.bin` (git-ignored via `*.bin`,
verified — it contains KL_SECRET at 0x0 and must never be committed) and loaded
relatively via `sysbus.spi2 LoadImage @external_flash.bin` (added to `boot.resc`).

`NrfSpim.cs` is now a **real SPI-NOR flash model** (no fidelity gap on content —
the running fw is 3411, exactly what the sim loads):
- `0x03` READ (24-bit addr) and `0x0B` FAST READ (addr+dummy) → copy `dump[addr..]`
  into the Rx buffer; `0x9F` RDID → C2 28 17 (MX25R6435F); `0x05` RDSR → 0
  (not busy); `0x66`/`0x99`/`0x06`/`0x04` → ack; others → benign default.
- Added a real IRQ line (`-> nvic@0x23`) that asserts on END when the SPIM END
  interrupt is enabled (`INTENSET` bit 6), for the async transfer path.
- Keeps the byte-stream + command-histogram capture (`DumpStream` / `DumpCmds`).

**STATUS — still blocked at the flash reset/detect, honestly.** With real content
loaded, boot is *still* stuck: the command histogram shows **only `0x66`
(Reset-Enable), ~2.4M times — it never reaches `0x99`, `0x9F`, or any READ**. So
it's blocked at the very first flash op, in the driver's async completion wait
after the reset command (`spi_xfer_wait` 0x9bda8 path; the detect polls
`*[detect_ctx+12]`). Tried, none broke it:
- raising the SPIM END NVIC IRQ (35) with VTOR→app enabled;
- setting the async flag `*[detect_ctx+12]=1` after the loop has run (crash-free
  but didn't advance);
- forcing the detect-check result (crashes downstream — the loop body sets up
  state that forcing skips).

So the async trigger that the flash driver waits on is still unpinned — it is
**not** the plain SPIM END (my IRQ/END don't satisfy it). Likely candidates to
chase next: a worker/DMA-completion task that must be scheduled (needs the tick +
that task actually running), or a second event source. Coverage does **not** yet
advance past flash init. The real flash model + content is in place for when the
trigger is found; that's the remaining gate before the display.

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

5. **Scheduler-drive hacks** (in `sched-experiment.resc`, not in `run-logs.resc`):
   `VectorTableOffset 0x27000` bypasses the SoftDevice's interrupt forwarding —
   **real SD IRQ forwarding is not exercised**; `wfe`→`nop` means **the real
   low-power idle/WFE wake path is not tested** (CPU busy-polls instead);
   NVIC `ISER` for IRQ36 hand-enabled because `sd_nvic_EnableIRQ` is not emulated;
   `SEVONPEND` forced; and a **synthetic tick is injected** by pending IRQ36
   periodically because the tickless idle doesn't reprogram RTC2 CC0 under these
   hacks — so **tick timing is fabricated, not derived from the RTC2 compare the
   firmware programs**. Anything depending on real tick cadence or on the SD's
   clock/timeslot behaviour is not validated.

6. **`wfe`(0x7400a)→nop, applied at boot** — stands in for Renode not honouring
   the firmware's SEVONPEND; the idle busy-polls instead of low-power sleeping.
   Real WFE/low-power idle path not exercised. This RETIRES the injected-tick
   hack (the tick now self-sustains from the real RTC2 compare).
7. **RTC1 TASKS_START force-written** — RTC1 is the busy-delay timebase; the
   reached boot never starts it. Stands in for the firmware's own RTC1 init
   (location/trigger untraced). Renode's RTC1 counts fine once started.
8. **SPIM END not raised** — Renode's `NRF52840_SPI` doesn't complete the OLED's
   EasyDMA transfer, so `spim_wait_end` (0x3aee2) spins. No clean workaround yet
   (writing EventsEnd=1 doesn't stick — events aren't set by register writes);
   the fix is model-level (make the SPI model raise END on TasksStart). Until
   then the OLED command stream past the first transfer isn't captured.

9. **SPIM END busy-spin patched (0x3aee8 `beq`→nop)** — used only in capture
   experiments to walk past `spim_wait_end`; it lets the small-transfer path
   proceed without a real END. NOT a fix (the driver then busy-polls the OLED
   controller instead). The real fix is an SPI-slave OLED model. Not in any
   committed run script.
10. **Echo SPI response injection** — an experiment writing the sent byte back
    into the Rx buffer to try to satisfy the 0x66 poll; inconclusive (run timed
    out). Documented so it isn't mistaken for a working model.

Rejected (broke boot, do not use): forcing *all* semaphore takes non-blocking;
clearing `BASEPRI`/`PRIMASK` with all IRQs enabled (interrupt storm); injecting
ticks by pending IRQ36 (replaced by the self-sustaining `wfe`→nop tick).

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

11. **Custom SPIM controller `NrfSpim.cs` (`SPI.NrfSpimCapture`)** replaces the
    stock `NRF52840_SPI` at spi2. It performs the EasyDMA and raises EVENTS_END
    (which the stock model didn't) — a real model, not a poke — but it answers
    the OLED poll with a single constant `PollReply` (a stand-in until the real
    detect response is pinned) and does not model true SPI slave timing/CS. The
    OLED command stream it records is faithful; the poll reply is synthetic.

12. **JEDEC id stubbed in `NrfSpim.cs`** — returns C2 28 17 (MX25R6435F) for 0x9F,
    0xFF (erased) for other reads. There is no external-flash **content** behind
    spi2, so any real flash data the firmware reads (dblib/config/assets) is wrong.
    A faithful sim needs a dump of the watch's external SPI flash loaded here.

13. **Real flash CONTENT is now faithful** (8 MB dump of the running 3411 fw's
    external flash) — not a gap. But the flash **detect/reset async handshake is
    not modelled/satisfied**, so boot doesn't yet pass flash init; the sim can't
    exercise anything downstream (display) until that async completion is driven.

14. **SPIM AMOUNT registers** (TXD.AMOUNT 0x54C / RXD.AMOUNT 0x53C) in
    `NrfSpim.cs` — a correctness FIX, not a gap: real nRF SPIM sets these to bytes
    transferred and the driver requires them. Without it the flash driver spun
    forever. Now faithful.
15. **OLED-init busy-delay loops patched (0x3bb96/0x3bbae `bpl`→nop)** — these are
    CPU-cycle hardware settle delays (µs–ms) that only waste sim wall-clock; the
    `wfe`→nop tick busy-poll already makes long runs slow. Skipping them does not
    change display behaviour, but note the sim no longer honours those exact
    settle times (a panel that needed them on real HW wouldn't be exercised).
