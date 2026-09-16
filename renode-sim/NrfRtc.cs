//
// RTC with the 24-bit counter overflow, wrapping Renode's NRF52840_RTC.
//
// The stock model leaves OVRFLW tagged: the event never sets, INTENSET bit 1 is
// dropped and no interrupt is raised when the counter wraps. The firmware's
// uptime clock is (overflow_count << 9) + (COUNTER >> 15) and the overflow count
// only moves in the RTC1 handler, so with the stock model the watch's seconds
// counter jumps back to 0 every 512 s. Every absolute deadline booked before the
// wrap (the algorithm manager's next-minute alarm, and through it the main task's
// wakeup) then sits in the future forever, main stops feeding software watchdog
// id 0, and the 90 s timeout resets the watch about ten minutes into a run.
//
// The interface implementations of the stock model are sealed, so this forwards
// to an instance of it and owns only the overflow: a limit timer that runs the
// counter's own 2^24 period, the event and enable bits, and the shared interrupt
// line, which follows the inner model's line ORed with a pending overflow.
//
using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Core.Structure;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.Miscellaneous;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Timers
{
    public sealed class NrfRtc : IDoubleWordPeripheral, IKnownSize, INRFEventProvider, IGPIOReceiver
    {
        public NrfRtc(IMachine machine, int numberOfEvents)
        {
            inner = new NRF52840_RTC(machine, numberOfEvents);
            inner.EventTriggered += e => EventTriggered?.Invoke(e);
            IRQ = new GPIO();
            inner.IRQ.Connect(this, 0);
            overflow = new LimitTimer(machine.ClockSource, BaseFrequency, this, "ovrflw", CounterPeriod, Direction.Ascending, false, WorkMode.Periodic, true, true);
            overflow.LimitReached += () =>
            {
                overflowEvent = true;
                EventTriggered?.Invoke(EventsOverflow);
                UpdateIrq();
            };
        }

        public GPIO IRQ { get; private set; }

        public event Action<uint> EventTriggered;

        public uint ReadDoubleWord(long offset)
        {
            if(offset == EventsOverflow)
            {
                return overflowEvent ? 1u : 0u;
            }
            var value = inner.ReadDoubleWord(offset);
            if(offset == IntenSet || offset == IntenClr)
            {
                value |= overflowEnabled ? OverflowBit : 0u;
            }
            return value;
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
            case EventsOverflow:
                overflowEvent = value != 0;
                UpdateIrq();
                return;
            case IntenSet:
                overflowEnabled |= (value & OverflowBit) != 0;
                break;
            case IntenClr:
                overflowEnabled &= (value & OverflowBit) == 0;
                break;
            case TasksStart:
                overflow.Enabled = true;
                break;
            case TasksStop:
                overflow.Enabled = false;
                break;
            case TasksClear:
                overflow.Value = 0;
                break;
            case Prescaler:
                overflow.Frequency = BaseFrequency / (value + 1);
                break;
            }
            inner.WriteDoubleWord(offset, value);
            UpdateIrq();
        }

        public void OnGPIO(int number, bool value)
        {
            innerIrq = value;
            UpdateIrq();
        }

        public void Reset()
        {
            inner.Reset();
            overflow.Reset();
            overflowEvent = false;
            overflowEnabled = false;
            innerIrq = false;
            UpdateIrq();
        }

        public long Size => inner.Size;

        private void UpdateIrq()
        {
            IRQ.Set(innerIrq || (overflowEvent && overflowEnabled));
        }

        private const long TasksStart = 0x000;
        private const long TasksStop = 0x004;
        private const long TasksClear = 0x008;
        private const uint EventsOverflow = 0x104;
        private const long IntenSet = 0x304;
        private const long IntenClr = 0x308;
        private const long Prescaler = 0x508;
        private const uint OverflowBit = 1u << 1;
        private const uint BaseFrequency = 32768;
        private const ulong CounterPeriod = 1ul << 24;

        private bool overflowEvent;
        private bool overflowEnabled;
        private bool innerIrq;
        private readonly LimitTimer overflow;
        private readonly NRF52840_RTC inner;
    }
}
