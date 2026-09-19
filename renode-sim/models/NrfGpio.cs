//
// Renode's stock NRF52840_GPIO leaves LATCH (0x520) and DETECTMODE (0x524) as
// tags. The watch's GPIOTE PORT handler identifies the pin that woke it by
// reading LATCH and acknowledges it by writing the bit back, so with an
// always-zero LATCH it dispatches nothing and the digital crown's MOTION line
// is never serviced.
//
// This derives from the stock model rather than copying it because
// NRF52840_GPIOTasksEvents takes its two ports as NRF52840_GPIO and consumes
// their PinChanged/Detect events; an independent copy could not be wired to the
// stock GPIOTE at all.
//
using System;
using System.Linq;

using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.GPIOPort
{
    public class NrfGpio : NRF52840_GPIO, IDoubleWordPeripheral
    {
        public NrfGpio(IMachine machine) : base(machine)
        {
            PinChanged += (_, __) => LatchSenseMatches();
        }

        public override void Reset()
        {
            base.Reset();
            latch = 0;
            detectSource = DetectSource.CurrentSenseMatches;
        }

        public new uint ReadDoubleWord(long offset)
        {
            switch((Registers)offset)
            {
            case Registers.Latch:
                return latch;
            case Registers.DetectMode:
                return (uint)detectSource;
            default:
                return base.ReadDoubleWord(offset);
            }
        }

        public new void WriteDoubleWord(long offset, uint value)
        {
            switch((Registers)offset)
            {
            case Registers.Latch:
                latch &= ~value;
                return;
            case Registers.DetectMode:
                if(value > (uint)DetectSource.LatchedSenseMatches)
                {
                    throw new Antmicro.Renode.Exceptions.RecoverableException(
                        string.Format("DETECTMODE has only bit 0 defined, got 0x{0:X}", value));
                }
                detectSource = (DetectSource)value;
                return;
            default:
                base.WriteDoubleWord(offset, value);
                break;
            }

            // A SENSE change can make a pin's current level a match without the
            // level itself moving, which is how a driver arms a pin that is
            // already at its active level.
            if(offset >= (long)Registers.PinConfigure)
            {
                LatchSenseMatches();
            }
        }

        private void LatchSenseMatches()
        {
            latch |= Pins.Where(pin => pin.IsSensing).Aggregate(0u, (bits, pin) => bits | (1u << pin.Id));
        }

        private uint latch;
        private DetectSource detectSource;

        // The DETECT edge that GPIOTE turns into the PORT event still comes from
        // the base model's OR of the current sense matches. That OR rises at the
        // same instant the OR of the LATCH bits does, because a latch bit is set
        // exactly when its pin starts matching, so both DETECTMODE settings
        // produce the same PORT event; they differ only in how long DETECT stays
        // asserted afterwards, which GPIOTE's sticky EVENTS_PORT hides.
        private enum DetectSource : uint
        {
            CurrentSenseMatches = 0,
            LatchedSenseMatches = 1,
        }

        private enum Registers : long
        {
            Latch = 0x20,
            DetectMode = 0x24,
            PinConfigure = 0x200,
        }
    }
}
