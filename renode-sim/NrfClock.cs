//
// nRF52840 CLOCK (POWER/CLOCK) minimal model. The stock Renode model may leave
// HFCLK/LFCLK reading "not running", which stalls any code (the SoftDevice's
// clock state machine, or the app polling sd_clock_hfclk_is_running) that waits
// for the clocks before bringing up a high-speed peripheral such as the display
// SPIM. This model completes a clock start immediately: on TASKS_*CLKSTART it
// raises the matching STARTED event, sets *CLKRUN/*CLKSTAT to running, and (when
// enabled) pulses the CLOCK IRQ. That mirrors real hardware, where a crystal
// settles in well under a scheduler tick.
//
using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Core.Structure;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    public class NrfClockReady : IDoubleWordPeripheral, IKnownSize, INumberedGPIOOutput
    {
        public NrfClockReady(IMachine machine)
        {
            this.machine = machine;
            regs = new Dictionary<long, uint>();
            IRQ = new GPIO();
            Connections = new Dictionary<int, IGPIO> { { 0, IRQ } };
            Reset();
        }

        public GPIO IRQ { get; private set; }
        public IReadOnlyDictionary<int, IGPIO> Connections { get; private set; }

        // POWER shares this block, and RESETREAS is the only part of it anything
        // reads: the bootloader treats a zero here as a power-on and throws away
        // the RAM control block the application left it. Renode has no reset
        // cause to report, so the run script sets this at the point it knows.
        public uint ResetReason
        {
            get { return Get(ResetReas); }
            set { regs[ResetReas] = value; }
        }

        public uint ReadDoubleWord(long offset)
        {
            uint v;
            return regs.TryGetValue(offset, out v) ? v : 0u;
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
            case TasksHfclkStart:
                regs[HfclkRun] = 1;
                regs[HfclkStat] = 0x00010001;   // STATE=running, SRC=Xtal
                regs[EventsHfclkStarted] = 1;
                inten |= 0; // no-op, keep style
                UpdateIrq();
                break;
            case TasksLfclkStart:
                regs[LfclkRun] = 1;
                regs[LfclkStat] = 0x00010000 | (regs.ContainsKey(LfclkSrc) ? (regs[LfclkSrc] & 0x3) : 0u); // STATE=running
                regs[EventsLfclkStarted] = 1;
                UpdateIrq();
                break;
            case TasksHfclkStop:
                regs[HfclkRun] = 0;
                regs[HfclkStat] = 0;
                break;
            case TasksLfclkStop:
                regs[LfclkRun] = 0;
                regs[LfclkStat] = 0;
                break;
            case EventsHfclkStarted:
            case EventsLfclkStarted:
                regs[offset] = value; // write 0 clears the event
                UpdateIrq();
                break;
            case ResetReas:
                regs[offset] &= ~value; // write-1-to-clear
                break;
            case IntenSet:
                inten |= value;
                UpdateIrq();
                break;
            case IntenClr:
                inten &= ~value;
                UpdateIrq();
                break;
            default:
                regs[offset] = value;
                break;
            }
        }

        private void UpdateIrq()
        {
            uint pending = 0;
            if(Get(EventsHfclkStarted) != 0) pending |= 1u << 0;
            if(Get(EventsLfclkStarted) != 0) pending |= 1u << 1;
            IRQ.Set((pending & inten) != 0);
        }

        public void Reset()
        {
            regs.Clear();
            inten = 0;
            IRQ.Set(false);
        }

        private uint Get(long off)
        {
            uint v;
            return regs.TryGetValue(off, out v) ? v : 0u;
        }

        public long Size { get { return 0x1000; } }

        private const long TasksHfclkStart = 0x000;
        private const long TasksHfclkStop = 0x004;
        private const long TasksLfclkStart = 0x008;
        private const long TasksLfclkStop = 0x00C;
        private const long EventsHfclkStarted = 0x100;
        private const long EventsLfclkStarted = 0x104;
        private const long ResetReas = 0x400;
        private const long IntenSet = 0x304;
        private const long IntenClr = 0x308;
        private const long HfclkRun = 0x408;
        private const long HfclkStat = 0x40C;
        private const long LfclkRun = 0x414;
        private const long LfclkStat = 0x418;
        private const long LfclkSrc = 0x518;

        private uint inten;
        private readonly Dictionary<long, uint> regs;
        private readonly IMachine machine;
    }
}
