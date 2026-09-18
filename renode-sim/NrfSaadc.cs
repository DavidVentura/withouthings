//
// nRF52840 SAADC. Renode 1.17 has no model for it, so the block fell through to
// the SVD stub and the firmware's battery read spun forever on EVENTS_STARTED.
// The conversion result is computed from the channel configuration the same way
// the silicon does, so the value the firmware reads tracks RESOLUTION/GAIN/REFSEL
// instead of being a fixed number the driver could never have produced.
//
// This block is also the watch's ECG front end. There is no separate analogue
// front end on the board: the firmware's ECG acquisition programs SAMPLERATE in
// timer mode, OVERSAMPLE, and a pair of EasyDMA buffers of fifty 16-bit results,
// and every block of samples the ECG module processes arrives that way. The
// battery read is the other user and it is task-triggered, which is the rule
// this model reads the input by: a conversion the internal timer drove takes the
// ECG waveform, a conversion TASKS_SAMPLE drove takes the channel's static
// input voltage.
//
using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Core.Structure;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.Timers;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Analog
{
    public class NrfSaadc : IDoubleWordPeripheral, IKnownSize, INumberedGPIOOutput
    {
        public NrfSaadc(IMachine machine)
        {
            this.machine = machine;
            this.sysbus = machine.GetSystemBus(this);
            regs = new Dictionary<long, uint>();
            inputVoltage = new double[ChannelCount];
            IRQ = new GPIO();
            Connections = new Dictionary<int, IGPIO> { { 0, IRQ } };
            Vdd = 3.0;
            EcgBpm = 62;
            EcgMillivolts = 110;
            sampleTimer = new LimitTimer(machine.ClockSource, TimerFrequency, this, "saadc-samplerate",
                limit: 1, direction: Antmicro.Renode.Time.Direction.Ascending, enabled: false,
                workMode: WorkMode.Periodic, eventEnabled: true);
            sampleTimer.LimitReached += TimedSample;
            Reset();
        }

        public GPIO IRQ { get; private set; }
        public IReadOnlyDictionary<int, IGPIO> Connections { get; private set; }

        // Supply rail, used as Vref/4 when a channel selects REFSEL=VDD1_4.
        public double Vdd { get; set; }

        // The synthetic ECG the internal timer's conversions read, in beats per
        // minute and in millivolts peak (the R wave) at the analogue input pin.
        // The electrode amplifier in front of that pin is off the chip and is
        // not modelled, so this is its output and not what a pair of electrodes
        // would see: the firmware programs AIN4 single-ended with gain 1/3
        // against the 0.6 V reference, which is 110 uV a code, and the default
        // is the millivolts that put a normal R wave about a thousand codes
        // above the baseline. Kept out of Reset() like the input voltages.
        public double EcgBpm { get; set; }
        public double EcgMillivolts { get; set; }

        // Analog input on AIN<channel>, in volts. Set from the run script; kept out
        // of Reset() so it survives the SYSRESETREQ the app's fault handler issues.
        public void SetInputVoltage(int channel, double volts)
        {
            if(channel < 0 || channel >= ChannelCount)
            {
                throw new RecoverableException(string.Format("channel must be 0..{0}", ChannelCount - 1));
            }
            inputVoltage[channel] = volts;
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
            case TasksStart:
                Start();
                return;
            case TasksSample:
                Sample();
                return;
            case TasksStop:
                Stop();
                return;
            case TasksCalibrateOffset:
                SetEvent(EventsCalibrateDone);
                return;
            case SampleRate:
                regs[offset] = value;
                if(bufferInProgress)
                {
                    StartSampleTimer();
                }
                return;
            case Inten:
                inten = value;
                UpdateIrq();
                return;
            case IntenSet:
                inten |= value;
                UpdateIrq();
                return;
            case IntenClr:
                inten &= ~value;
                UpdateIrq();
                return;
            }
            regs[offset] = value;
            if(offset >= EventsStarted && offset <= EventsStopped)
            {
                UpdateIrq();
            }
        }

        private void Start()
        {
            if(Get(Enable) == 0)
            {
                this.Log(LogLevel.Warning, "TASKS_START with ENABLE=0, ignoring");
                return;
            }
            samplePosition = 0;
            regs[ResultAmount] = 0;
            bufferInProgress = true;
            SetEvent(EventsStarted);
            StartSampleTimer();
        }

        // SAMPLERATE selects between a conversion per TASKS_SAMPLE and a
        // conversion per CC ticks of the 16 MHz clock; OVERSAMPLE accumulates
        // 2^N conversions into one result, so the rate a result appears at is
        // the timer's divided by that. The firmware's ECG programs CC 1667 and
        // OVERSAMPLE 32, which is the 300 Hz the measurement's own metadata
        // reports, and its battery read leaves MODE at task and is unaffected.
        private void StartSampleTimer()
        {
            uint samplerate = Get(SampleRate);
            if((samplerate & SampleRateTimerMode) == 0)
            {
                sampleTimer.Enabled = false;
                return;
            }
            uint cc = samplerate & 0xFFF;
            if(cc == 0)
            {
                this.Log(LogLevel.Warning, "SAMPLERATE is in timer mode with CC=0, so nothing samples");
                return;
            }
            double rate = AdcClockHz / (cc * (double)(1 << (int)(Get(Oversample) & 0xF)));
            sampleTimer.Limit = (ulong)Math.Max(1, Math.Round(TimerFrequency / rate));
            sampleTimer.Enabled = true;
            var on = new List<string>();
            for(int channel = 0; channel < ChannelCount; channel++)
            {
                if(Get(ChannelPselP(channel)) != 0)
                {
                    on.Add(string.Format("CH{0} PSELP={1} PSELN={2} CONFIG=0x{3:X}", channel,
                        Get(ChannelPselP(channel)), Get(ChannelPselN(channel)),
                        Get(ChannelConfig(channel))));
                }
            }
            this.Log(LogLevel.Info, "sampling into 0x{0:X} for {1} results at {2:F1} Hz, {3} bits, {4}",
                Get(ResultPtr), Get(ResultMaxCnt), rate, ResolutionBits(), string.Join(", ", on));
        }

        private void TimedSample()
        {
            Sample(fromTimer: true);
        }

        private void Sample(bool fromTimer = false)
        {
            if(Get(Enable) == 0)
            {
                this.Log(LogLevel.Warning, "TASKS_SAMPLE with ENABLE=0, ignoring");
                return;
            }
            uint maxCount = Get(ResultMaxCnt);
            ulong pointer = Get(ResultPtr);
            for(int channel = 0; channel < ChannelCount; channel++)
            {
                if(Get(ChannelPselP(channel)) == 0)
                {
                    continue;
                }
                if(samplePosition >= maxCount)
                {
                    this.Log(LogLevel.Warning, "sample buffer full ({0} entries), dropping channel {1}", maxCount, channel);
                    break;
                }
                short result = Convert(channel, fromTimer);
                sysbus.WriteByte(pointer + (ulong)(2 * samplePosition), (byte)(result & 0xFF));
                sysbus.WriteByte(pointer + (ulong)(2 * samplePosition) + 1, (byte)((result >> 8) & 0xFF));
                samplePosition++;
                regs[ResultAmount] = samplePosition;
            }
            SetEvent(EventsDone);
            SetEvent(EventsResultDone);
            if(samplePosition >= maxCount)
            {
                bufferInProgress = false;
                // One buffer is one END; the driver hands the next buffer over
                // in the handler and starts again, so the timer stops here
                // rather than running on into a pointer nothing owns.
                sampleTimer.Enabled = false;
                SetEvent(EventsEnd);
            }
        }

        private void Stop()
        {
            sampleTimer.Enabled = false;
            if(bufferInProgress)
            {
                bufferInProgress = false;
                SetEvent(EventsEnd);
            }
            SetEvent(EventsStopped);
        }

        private short Convert(int channel, bool fromTimer = false)
        {
            uint config = Get(ChannelConfig(channel));
            double gain = GainFactor((config >> 8) & 0x7, channel);
            double reference = ((config >> 12) & 0x1) == 0 ? InternalReference : Vdd / 4.0;
            int bits = ResolutionBits();
            // The ECG amplifier's output sits at mid-scale, which is what the
            // measurement's own UnitConversionParameters says when it reports an
            // offset of -8192 against a 14-bit result.
            double input = fromTimer ? reference / gain / 2.0 + EcgVolts() : inputVoltage[channel];
            bool differential = ((config >> 20) & 0x1) != 0;
            if(differential)
            {
                uint negative = Get(ChannelPselN(channel));
                double negativeInput = negative == 0 ? 0.0 : inputVoltage[(int)(negative - 1) % ChannelCount];
                return Clamp((input - negativeInput) * gain / reference * (1 << (bits - 1)), -(1 << (bits - 1)), (1 << (bits - 1)) - 1);
            }
            return Clamp(input * gain / reference * (1 << bits), 0, (1 << bits) - 1);
        }

        // One beat of a synthetic lead-I ECG at the current virtual time: a P
        // wave, a QRS complex and a T wave, each a Gaussian at its own place in
        // the beat. The shape matters and the exact millivolts do not -- what
        // the firmware's chain looks for is a sharp R peak at a repeatable
        // interval, which is what makes a beat detector and an RR series
        // testable at all.
        private double EcgVolts()
        {
            double period = 60.0 / Math.Max(20.0, EcgBpm);
            double seconds = machine.ElapsedVirtualTime.TimeElapsed.TotalSeconds;
            double t = seconds - Math.Floor(seconds / period) * period;
            double r = EcgMillivolts / 1000.0;
            return Bump(t, 0.16 * period, 0.025, 0.15 * r)      // P
                 - Bump(t, 0.24 * period, 0.008, 0.10 * r)      // Q
                 + Bump(t, 0.26 * period, 0.008, r)             // R
                 - Bump(t, 0.29 * period, 0.010, 0.20 * r)      // S
                 + Bump(t, 0.45 * period, 0.045, 0.30 * r);     // T
        }

        private static double Bump(double t, double centre, double width, double height)
        {
            double z = (t - centre) / width;
            return height * Math.Exp(-0.5 * z * z);
        }

        private double GainFactor(uint code, int channel)
        {
            switch(code)
            {
            case 0: return 1.0 / 6.0;
            case 1: return 1.0 / 5.0;
            case 2: return 1.0 / 4.0;
            case 3: return 1.0 / 3.0;
            case 4: return 1.0 / 2.0;
            case 5: return 1.0;
            case 6: return 2.0;
            case 7: return 4.0;
            }
            throw new InvalidOperationException(string.Format("unreachable gain code {0} on channel {1}", code, channel));
        }

        private int ResolutionBits()
        {
            uint resolution = Get(Resolution) & 0x3;
            switch(resolution)
            {
            case 0: return 8;
            case 1: return 10;
            case 2: return 12;
            case 3: return 14;
            }
            throw new InvalidOperationException("unreachable resolution code");
        }

        private short Clamp(double value, int low, int high)
        {
            long rounded = (long)Math.Round(value);
            if(rounded > high)
            {
                return (short)high;
            }
            if(rounded < low)
            {
                return (short)low;
            }
            return (short)rounded;
        }

        private void SetEvent(long offset)
        {
            regs[offset] = 1;
            UpdateIrq();
        }

        private void UpdateIrq()
        {
            uint pending = 0;
            if(Get(EventsStarted) != 0) pending |= 1u << 0;
            if(Get(EventsEnd) != 0) pending |= 1u << 1;
            if(Get(EventsDone) != 0) pending |= 1u << 2;
            if(Get(EventsResultDone) != 0) pending |= 1u << 3;
            if(Get(EventsCalibrateDone) != 0) pending |= 1u << 4;
            if(Get(EventsStopped) != 0) pending |= 1u << 5;
            IRQ.Set((pending & inten) != 0);
        }

        public void Reset()
        {
            regs.Clear();
            sampleTimer.Enabled = false;
            inten = 0;
            samplePosition = 0;
            bufferInProgress = false;
            IRQ.Set(false);
        }

        private uint Get(long offset)
        {
            uint v;
            return regs.TryGetValue(offset, out v) ? v : 0u;
        }

        public long Size { get { return 0x1000; } }

        private static long ChannelPselP(int channel) { return 0x510 + 0x10 * channel; }
        private static long ChannelPselN(int channel) { return 0x514 + 0x10 * channel; }
        private static long ChannelConfig(int channel) { return 0x518 + 0x10 * channel; }

        private const int ChannelCount = 8;
        private const double InternalReference = 0.6;

        private const long TasksStart = 0x000;
        private const long TasksSample = 0x004;
        private const long TasksStop = 0x008;
        private const long TasksCalibrateOffset = 0x00C;
        private const long EventsStarted = 0x100;
        private const long EventsEnd = 0x104;
        private const long EventsDone = 0x108;
        private const long EventsResultDone = 0x10C;
        private const long EventsCalibrateDone = 0x110;
        private const long EventsStopped = 0x114;
        private const long Inten = 0x300;
        private const long IntenSet = 0x304;
        private const long IntenClr = 0x308;
        private const long Enable = 0x500;
        private const long Resolution = 0x5F0;
        private const long Oversample = 0x5F4;
        private const long SampleRate = 0x5F8;
        private const uint SampleRateTimerMode = 1u << 12;
        // The SAADC's own 16 MHz clock, which SAMPLERATE.CC divides.
        private const double AdcClockHz = 16000000.0;
        private const long TimerFrequency = 1000000;
        private const long ResultPtr = 0x62C;
        private const long ResultMaxCnt = 0x630;
        private const long ResultAmount = 0x634;

        private uint inten;
        private uint samplePosition;
        private bool bufferInProgress;
        private readonly double[] inputVoltage;
        private readonly LimitTimer sampleTimer;
        private readonly Dictionary<long, uint> regs;
        private readonly IBusController sysbus;
        private readonly IMachine machine;
    }
}
