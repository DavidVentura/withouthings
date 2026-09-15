//
// nRF52840 SAADC. Renode 1.17 has no model for it, so the block fell through to
// the SVD stub and the firmware's battery read spun forever on EVENTS_STARTED.
// The conversion result is computed from the channel configuration the same way
// the silicon does, so the value the firmware reads tracks RESOLUTION/GAIN/REFSEL
// instead of being a fixed number the driver could never have produced.
//
using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Core.Structure;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Bus;

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
            Reset();
        }

        public GPIO IRQ { get; private set; }
        public IReadOnlyDictionary<int, IGPIO> Connections { get; private set; }

        // Supply rail, used as Vref/4 when a channel selects REFSEL=VDD1_4.
        public double Vdd { get; set; }

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
        }

        private void Sample()
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
                short result = Convert(channel);
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
                SetEvent(EventsEnd);
            }
        }

        private void Stop()
        {
            if(bufferInProgress)
            {
                bufferInProgress = false;
                SetEvent(EventsEnd);
            }
            SetEvent(EventsStopped);
        }

        private short Convert(int channel)
        {
            uint config = Get(ChannelConfig(channel));
            double gain = GainFactor((config >> 8) & 0x7, channel);
            double reference = ((config >> 12) & 0x1) == 0 ? InternalReference : Vdd / 4.0;
            int bits = ResolutionBits();
            double input = inputVoltage[channel];
            bool differential = ((config >> 20) & 0x1) != 0;
            if(differential)
            {
                uint negative = Get(ChannelPselN(channel));
                double negativeInput = negative == 0 ? 0.0 : inputVoltage[(int)(negative - 1) % ChannelCount];
                return Clamp((input - negativeInput) * gain / reference * (1 << (bits - 1)), -(1 << (bits - 1)), (1 << (bits - 1)) - 1);
            }
            return Clamp(input * gain / reference * (1 << bits), 0, (1 << bits) - 1);
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
        private const long ResultPtr = 0x62C;
        private const long ResultMaxCnt = 0x630;
        private const long ResultAmount = 0x634;

        private uint inten;
        private uint samplePosition;
        private bool bufferInProgress;
        private readonly double[] inputVoltage;
        private readonly Dictionary<long, uint> regs;
        private readonly IBusController sysbus;
        private readonly IMachine machine;
    }
}
