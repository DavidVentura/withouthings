//
// The three mechanical hands of the ScanWatch 2, decoded from the PWM
// sequences the step motor driver plays.
//
// Each hand is a Lavet-type stepper on its own PWM instance, wired with three
// lines: the two ends of the winding on channels 0 and 2 and the common on
// channel 1. The driver never touches the pins directly; one step is one
// nrfx_pwm complex playback whose first sequence is a single WaveForm-mode
// entry -- {ch0, ch1, ch2, COUNTERTOP} -- so the whole step is four halfwords
// in RAM and the pin levels never have to be sampled.
//
// In that entry the two winding ends carry the same compare value with
// opposite polarity bits, which is how a drive pulse is told apart from any
// other PWM use of the same instance, and the common carries a compare that
// resolves to a constant level for the period: 0 with the polarity bit set is
// high all period, a compare above COUNTERTOP is low all period. The end that
// is driven and the rail the common sits on together pick one of the four
// waveforms the driver builds at init; the pair {end polarity, common level}
// is equal for the two waveforms of one direction and unequal for the other,
// so the direction of a step is one exclusive or.
//
// A step is a step: the motor has 360 of them per revolution and the firmware
// counts positions in the same unit, so a position is also the hand's angle on
// the dial in degrees.
//
using System;
using System.Collections.Generic;
using System.Linq;
using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    public class NrfHands : IPeripheral
    {
        public NrfHands(IMachine machine, string hourPins, string minutePins, string trackerPins)
        {
            this.machine = machine;
            motors = new[]
            {
                new Motor(Hand.Hour, ParsePins(hourPins)),
                new Motor(Hand.Minute, ParsePins(minutePins)),
                new Motor(Hand.Tracker, ParsePins(trackerPins)),
            };
            Reset();
        }

        // Called by NrfPwm for every SEQSTART on sequence 0. Anything that is
        // not a drive pulse on a hand's three pins is not a step.
        public void PlayedSequence(uint pselOut0, uint pselOut1, uint pselOut2,
                                   ushort channel0, ushort channel1, ushort channel2, ushort counterTop)
        {
            var motor = motors.FirstOrDefault(m => m.Matches(pselOut0, pselOut1, pselOut2));
            if(motor == null)
            {
                return;
            }
            if((channel0 & CompareMask) != (channel2 & CompareMask) || ((channel0 ^ channel2) & PolarityBit) == 0)
            {
                this.Log(LogLevel.Warning, "{0}: PWM sequence {1:X4},{2:X4},{3:X4} is not a drive pulse, ignored",
                         motor.Hand, channel0, channel1, channel2);
                return;
            }

            var endInverted = (channel0 & PolarityBit) != 0;
            var commonHigh = (channel1 & CompareMask) == 0;
            var step = endInverted == commonHigh ? 1 : -1;

            var now = machine.ElapsedVirtualTime.TimeElapsed;
            if(motor.Pending != 0 && (motor.Direction != step || now - motor.LastStep > BatchGap))
            {
                Flush(motor);
            }
            motor.Direction = step;
            motor.Pending += 1;
            motor.LastStep = now;
            motor.Position = Wrap(motor.Position + step);
            motor.NetSteps += step;
            this.Log(LogLevel.Noisy, "{0}: step {1:+#;-#} to {2} deg, pulse {3} us of {4} us",
                     motor.Hand, step, motor.Position,
                     (channel0 & CompareMask) * TickMicroseconds, (counterTop + 1) * TickMicroseconds);
        }

        // The firmware restores the hand positions from its own store before it
        // takes the first step, so a run that wants absolute angles rather than
        // travel tells the model where the hands were left.
        public void SetPosition(string hand, int degrees)
        {
            var motor = Find(hand);
            Flush(motor);
            motor.Position = Wrap(degrees);
            motor.NetSteps = 0;
            this.Log(LogLevel.Info, "{0}: position set to {1} deg", motor.Hand, motor.Position);
        }

        public void LogPositions()
        {
            foreach(var motor in motors)
            {
                Flush(motor);
            }
            this.Log(LogLevel.Info, "hands at hour={0} deg ({1}), minute={2} deg ({3} min), tracker={4} deg; travel {5}/{6}/{7} steps",
                     HourAngle, HourOfDay, MinuteAngle, MinuteOfHour, TrackerAngle,
                     Find("hour").NetSteps, Find("minute").NetSteps, Find("tracker").NetSteps);
        }

        public int HourAngle { get { return Find("hour").Position; } }
        public int MinuteAngle { get { return Find("minute").Position; } }
        public int TrackerAngle { get { return Find("tracker").Position; } }

        // The hour hand turns once in twelve hours and the minute hand once in
        // one, both over the same 360 steps.
        public string HourOfDay
        {
            get
            {
                var minutes = HourAngle * 720 / StepsPerRevolution;
                return string.Format("{0:D2}h{1:D2}", minutes / 60, minutes % 60);
            }
        }

        public int MinuteOfHour { get { return MinuteAngle * 60 / StepsPerRevolution; } }

        public void Reset()
        {
            foreach(var motor in motors)
            {
                motor.Position = 0;
                motor.NetSteps = 0;
                motor.Pending = 0;
                motor.Direction = 0;
                motor.LastStep = TimeInterval.Empty;
            }
        }

        private void Flush(Motor motor)
        {
            if(motor.Pending == 0)
            {
                return;
            }
            this.Log(LogLevel.Info, "{0}: {1} steps {2} to {3} deg", motor.Hand, motor.Pending,
                     motor.Direction > 0 ? "clockwise" : "counter-clockwise", motor.Position);
            motor.Pending = 0;
        }

        private Motor Find(string hand)
        {
            Hand parsed;
            if(!Enum.TryParse(hand, true, out parsed))
            {
                throw new RecoverableException(string.Format("no such hand: {0}", hand));
            }
            return motors.First(m => m.Hand == parsed);
        }

        private static int Wrap(int position)
        {
            return ((position % StepsPerRevolution) + StepsPerRevolution) % StepsPerRevolution;
        }

        // "P1.2 P1.5 P1.6": winding end, common, winding end, in PWM channel
        // order, as PSEL.OUT encodes them.
        private static uint[] ParsePins(string pins)
        {
            var fields = pins.Split(new[] { ' ', ',' }, StringSplitOptions.RemoveEmptyEntries);
            if(fields.Length != 3)
            {
                throw new ConstructionException(string.Format("a motor has three pins, got '{0}'", pins));
            }
            return fields.Select(field =>
            {
                var parts = field.TrimStart('P', 'p').Split('.');
                uint port, pin;
                if(parts.Length != 2 || !uint.TryParse(parts[0], out port) || !uint.TryParse(parts[1], out pin)
                   || port > 1 || pin > 31)
                {
                    throw new ConstructionException(string.Format("not a pin: {0}", field));
                }
                return (port << 5) | pin;
            }).ToArray();
        }

        private class Motor
        {
            public Motor(Hand hand, uint[] pins)
            {
                Hand = hand;
                this.pins = pins;
            }

            public bool Matches(uint pselOut0, uint pselOut1, uint pselOut2)
            {
                return pins[0] == pselOut0 && pins[1] == pselOut1 && pins[2] == pselOut2;
            }

            public Hand Hand { get; private set; }
            public int Position { get; set; }
            public int NetSteps { get; set; }
            public int Pending { get; set; }
            public int Direction { get; set; }
            public TimeInterval LastStep { get; set; }

            private readonly uint[] pins;
        }

        private enum Hand
        {
            Hour,
            Minute,
            Tracker,
        }

        public const int StepsPerRevolution = 360;

        // PRESCALER 5 divides the 16 MHz PWM clock to 500 kHz.
        private const int TickMicroseconds = 2;
        private const ushort PolarityBit = 0x8000;
        private const ushort CompareMask = 0x7FFF;
        private static readonly TimeInterval BatchGap = TimeInterval.FromMilliseconds(200);

        private readonly Motor[] motors;
        private readonly IMachine machine;
    }
}
