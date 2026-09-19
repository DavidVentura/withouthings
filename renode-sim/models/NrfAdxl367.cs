//
// Analog Devices ADXL367 triaxial accelerometer, on SPIM2 with the flash, chip
// select P0.16, INT1 on P0.27. It is the motion input for wrist raise, steps,
// activity, sleep and worn detection.
//
// Framing is the ADXL362/367 one: a command byte (0x0A write registers, 0x0B
// read registers, 0x0D read FIFO), then for the register commands the start
// address, then bytes with the address auto-incrementing. The FIFO command
// takes no address; the driver holds chip select over several EasyDMA
// transfers and drains the FIFO in 30-byte chunks (0x94104..0x94126), so the
// frame state lives in this object and ends when chip select rises.
//
// The firmware only enforces DEVID_AD (0x00 = 0xAD, checked at 0x94376); it
// reads 0x03 and 0x0B at 0x93e76/0x93e98 but only prints them.
//
using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.SPI;
using Antmicro.Renode.Peripherals.Timers;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Sensors
{
    public class Adxl367 : ISPIPeripheral, INumberedGPIOOutput
    {
        public Adxl367(IMachine machine)
        {
            registers = new byte[RegisterCount];
            fifo = new Queue<ushort>();
            unknownRegisters = new HashSet<byte>();
            INT1 = new GPIO();
            INT2 = new GPIO();
            Connections = new Dictionary<int, IGPIO> { { 0, INT1 }, { 1, INT2 } };
            sampleTimer = new LimitTimer(machine.ClockSource, TimerFrequency, this, "adxl367-odr", limit: 1,
                direction: Direction.Ascending, enabled: false, workMode: WorkMode.Periodic, eventEnabled: true);
            sampleTimer.LimitReached += Sample;
            LoadDefaults();
        }

        public GPIO INT1 { get; private set; }
        public GPIO INT2 { get; private set; }
        public IReadOnlyDictionary<int, IGPIO> Connections { get; private set; }

        // The orientation the part rests at, in milli-g, settable from the run
        // script. The default is the watch lying flat: gravity on +Z. Kept out of
        // Reset() so it survives the SYSRESETREQ the app's fault handler issues.
        public int MilligX { get; set; }
        public int MilligY { get; set; }
        public int MilligZ { get; set; } = 1000;

        // A wrist raise: gravity rotates out of +Z into +Y over the given time,
        // and the watch stays in the raised orientation afterwards. The samples
        // are queued here and consumed at the ODR, so the firmware sees a real
        // ramp rather than one jump, which is what its raise detector looks for.
        public void Motion(int milliseconds = 800, int degrees = 75)
        {
            if(milliseconds <= 0)
            {
                throw new RecoverableException("the gesture needs a positive duration");
            }
            var steps = Math.Max(1, (int)(milliseconds * CurrentOutputDataRate / 1000.0));
            var radians = degrees * Math.PI / 180.0;
            var startY = MilligY;
            var startZ = MilligZ;
            gesture.Clear();
            for(var step = 1; step <= steps; step++)
            {
                var angle = radians * step / steps;
                var y = startY * Math.Cos(angle) + startZ * Math.Sin(angle);
                var z = startZ * Math.Cos(angle) - startY * Math.Sin(angle);
                gesture.Enqueue(new Vector(MilligX, (int)Math.Round(y), (int)Math.Round(z)));
            }
            var last = new Vector(MilligX,
                (int)Math.Round(startY * Math.Cos(radians) + startZ * Math.Sin(radians)),
                (int)Math.Round(startZ * Math.Cos(radians) - startY * Math.Sin(radians)));
            MilligY = last.Y;
            MilligZ = last.Z;
            this.Log(LogLevel.Info, "wrist raise queued: {0} samples at {1} Hz, gravity ends at ({2}, {3}, {4}) mg",
                gesture.Count, CurrentOutputDataRate, last.X, last.Y, last.Z);
        }

        public byte Transmit(byte data)
        {
            var index = frameIndex;
            frameIndex++;
            if(index == 0)
            {
                command = (Command)data;
                if(command != Command.WriteRegister && command != Command.ReadRegister && command != Command.ReadFifo)
                {
                    this.Log(LogLevel.Warning, "unknown command 0x{0:X2}, the part answers zero for the rest of the frame", data);
                }
                return 0x00;
            }
            if(command == Command.ReadFifo)
            {
                return NextFifoByte();
            }
            if(index == 1)
            {
                address = data;
                return 0x00;
            }
            var target = (byte)(address + index - 2);
            if(command == Command.WriteRegister)
            {
                WriteRegister(target, data);
                return 0x00;
            }
            return command == Command.ReadRegister ? ReadRegister(target) : (byte)0x00;
        }

        public void FinishTransmission()
        {
            frameIndex = 0;
            command = Command.None;
        }

        public void Reset()
        {
            sampleTimer.Enabled = false;
            fifo.Clear();
            gesture.Clear();
            unknownRegisters.Clear();
            FinishTransmission();
            LoadDefaults();
        }

        private void LoadDefaults()
        {
            Array.Clear(registers, 0, registers.Length);
            registers[(int)Register.DeviceIdAd] = DeviceIdAd;
            registers[(int)Register.DeviceIdMst] = DeviceIdMst;
            registers[(int)Register.PartId] = PartId;
            registers[(int)Register.RevId] = RevId;
            registers[(int)Register.FilterControl] = DefaultFilterControl;
            // The part comes out of reset in standby but not asleep, and the
            // firmware's pre-power-on check reads INT1 with AWAKE mapped both
            // ways before it ever enables measurement (0x3bbe4).
            awake = true;
            activeReference = null;
            inactiveSamples = 0;
            UpdateStatus();
        }

        private byte ReadRegister(byte target)
        {
            switch((Register)target)
            {
            case Register.FifoEntriesLow:
                return (byte)(FifoSampleCount & 0xFF);
            case Register.FifoEntriesHigh:
                return (byte)((FifoSampleCount >> 8) & 0x03);
            case Register.Status:
                // Reading STATUS clears the sticky activity bits, as the part does.
                var status = registers[(int)Register.Status];
                registers[(int)Register.Status] = (byte)(status & ~StickyStatusBits);
                UpdateInterrupts();
                return status;
            case Register.XDataHigh:
                dataReady = false;
                UpdateStatus();
                return registers[target];
            default:
                WarnUnknown(target, "read");
                return registers[target];
            }
        }

        private void WriteRegister(byte target, byte value)
        {
            if(target == (byte)Register.SoftReset)
            {
                if(value != SoftResetKey)
                {
                    this.Log(LogLevel.Warning, "SOFT_RESET written 0x{0:X2}, only 0x52 resets the part", value);
                    return;
                }
                Reset();
                return;
            }
            if(target <= (byte)Register.Status || (target >= (byte)Register.XDataHigh && target <= (byte)Register.AdcLow))
            {
                this.Log(LogLevel.Warning, "write 0x{0:X2} to read-only register 0x{1:X2}", value, target);
                return;
            }
            WarnUnknown(target, "write");
            registers[target] = value;
            switch((Register)target)
            {
            case Register.PowerControl:
                UpdateMeasurementMode();
                return;
            case Register.FilterControl:
                sampleTimer.Limit = Math.Max(1, (ulong)(TimerFrequency / CurrentOutputDataRate));
                return;
            case Register.FifoControl:
                fifo.Clear();
                UpdateStatus();
                return;
            case Register.IntMap1:
            case Register.IntMap2:
                UpdateInterrupts();
                return;
            default:
                return;
            }
        }

        private void WarnUnknown(byte target, string access)
        {
            if(Enum.IsDefined(typeof(Register), (Register)target) || !unknownRegisters.Add(target))
            {
                return;
            }
            this.Log(LogLevel.Warning, "{0} of register 0x{1:X2}, which this model does not implement", access, target);
        }

        private void UpdateMeasurementMode()
        {
            var measuring = (registers[(int)Register.PowerControl] & PowerControlModeMask) == PowerControlMeasure;
            if(measuring == sampleTimer.Enabled)
            {
                return;
            }
            if(measuring)
            {
                activeReference = null;
                inactiveSamples = 0;
                sampleTimer.Limit = Math.Max(1, (ulong)(TimerFrequency / CurrentOutputDataRate));
                sampleTimer.ResetValue();
            }
            sampleTimer.Enabled = measuring;
            this.Log(LogLevel.Info, "{0} at {1} Hz, +/-{2} g, FIFO {3} with a {4} sample watermark",
                measuring ? "measuring" : "standby", CurrentOutputDataRate, CurrentRangeG, CurrentFifoMode, FifoWatermark);
        }

        private void Sample()
        {
            var vector = gesture.Count > 0 ? gesture.Dequeue() : new Vector(MilligX, MilligY, MilligZ);
            var x = ToCode(vector.X);
            var y = ToCode(vector.Y);
            var z = ToCode(vector.Z);
            StoreAxis(Register.XDataHigh, x);
            StoreAxis(Register.YDataHigh, y);
            StoreAxis(Register.ZDataHigh, z);
            DetectActivity(x, y, z);
            PushFifo(x, y, z);
            dataReady = true;
            UpdateStatus();
        }

        private void DetectActivity(short x, short y, short z)
        {
            var control = registers[(int)Register.ActInactControl];
            if(activeReference == null)
            {
                activeReference = new Vector(x, y, z);
                return;
            }
            var reference = activeReference.Value;
            var deltaX = Math.Abs(x - reference.X);
            var deltaY = Math.Abs(y - reference.Y);
            var deltaZ = Math.Abs(z - reference.Z);
            var largest = Math.Max(deltaX, Math.Max(deltaY, deltaZ));
            if((control & ActivityEnable) != 0 && largest > Threshold(Register.ThreshActHigh))
            {
                registers[(int)Register.Status] |= 1 << (int)StatusBit.Activity;
                awake = true;
                inactiveSamples = 0;
                activeReference = new Vector(x, y, z);
                return;
            }
            if((control & InactivityEnable) == 0 || largest > Threshold(Register.ThreshInactHigh))
            {
                inactiveSamples = 0;
                return;
            }
            inactiveSamples++;
            if(inactiveSamples < InactivityTime)
            {
                return;
            }
            inactiveSamples = 0;
            registers[(int)Register.Status] |= 1 << (int)StatusBit.Inactivity;
            awake = false;
        }

        private void PushFifo(short x, short y, short z)
        {
            if(CurrentFifoMode == FifoMode.Disabled)
            {
                return;
            }
            PushEntry(AxisTag.X, x);
            PushEntry(AxisTag.Y, y);
            PushEntry(AxisTag.Z, z);
        }

        private void PushEntry(AxisTag tag, short code)
        {
            if(fifo.Count >= FifoDepth)
            {
                registers[(int)Register.Status] |= 1 << (int)StatusBit.FifoOverrun;
                if(CurrentFifoMode == FifoMode.OldestSaved)
                {
                    return;
                }
                fifo.Dequeue();
            }
            // 14-bit read mode: the two tag bits ride in the top of the entry and
            // the driver resynchronises on them (0x94160). In 8-bit mode only the
            // high byte is kept, so the tag and the top six data bits stay put.
            fifo.Enqueue((ushort)(((ushort)tag << 14) | (ushort)(code & 0x3FFF)));
        }

        private byte NextFifoByte()
        {
            if(pendingLowByte.HasValue)
            {
                var low = pendingLowByte.Value;
                pendingLowByte = null;
                return low;
            }
            if(fifo.Count == 0)
            {
                return 0x00;
            }
            var entry = fifo.Dequeue();
            if(CurrentFifoReadMode == FifoReadMode.FourteenBit)
            {
                pendingLowByte = (byte)(entry & 0xFF);
            }
            UpdateStatus();
            return (byte)(entry >> 8);
        }

        private void StoreAxis(Register high, short code)
        {
            // The data registers hold the 14-bit sample left-justified in 16 bits,
            // MSB first; the driver reads them big-endian and shifts right by two.
            var justified = (ushort)(code << 2);
            registers[(int)high] = (byte)(justified >> 8);
            registers[(int)high + 1] = (byte)(justified & 0xFF);
        }

        private short ToCode(int milligs)
        {
            var code = (long)milligs * FullScaleCodes / (CurrentRangeG * 1000);
            return (short)Math.Max(-FullScaleCodes, Math.Min(FullScaleCodes - 1, code));
        }

        private int Threshold(Register high)
        {
            return ((registers[(int)high] & 0x7F) << 6) | (registers[(int)high + 1] >> 2);
        }

        private void UpdateStatus()
        {
            var status = (byte)(registers[(int)Register.Status] & ~LiveStatusBits);
            if(dataReady)
            {
                status |= 1 << (int)StatusBit.DataReady;
            }
            if(fifo.Count > 0)
            {
                status |= 1 << (int)StatusBit.FifoReady;
            }
            if(FifoSampleCount >= FifoWatermark)
            {
                status |= 1 << (int)StatusBit.FifoWatermark;
            }
            if(awake)
            {
                status |= 1 << (int)StatusBit.Awake;
            }
            registers[(int)Register.Status] = status;
            UpdateInterrupts();
        }

        private void UpdateInterrupts()
        {
            INT1.Set(InterruptLevel(registers[(int)Register.IntMap1]));
            INT2.Set(InterruptLevel(registers[(int)Register.IntMap2]));
        }

        private bool InterruptLevel(byte map)
        {
            var asserted = (registers[(int)Register.Status] & map & InterruptSourceMask) != 0;
            return (map & InterruptActiveLow) != 0 ? !asserted : asserted;
        }

        private int FifoSampleCount => fifo.Count;

        private int FifoWatermark => registers[(int)Register.FifoSamples]
            | ((registers[(int)Register.FifoControl] & FifoSamplesHighBit) != 0 ? 0x100 : 0);

        private FifoMode CurrentFifoMode => (FifoMode)(registers[(int)Register.FifoControl] & FifoModeMask);

        private FifoReadMode CurrentFifoReadMode => (registers[(int)Register.FifoControl] & FifoEightBitRead) != 0
            ? FifoReadMode.EightBit : FifoReadMode.FourteenBit;

        private int CurrentRangeG
        {
            get
            {
                var range = (registers[(int)Register.FilterControl] & RangeMask) >> RangeShift;
                if(range > 2)
                {
                    this.Log(LogLevel.Error, "FILTER_CTL selects the reserved range 0x{0:X}, reading samples at +/-8 g", range);
                    return 8;
                }
                return 2 << range;
            }
        }

        private double CurrentOutputDataRate
        {
            get
            {
                var code = registers[(int)Register.FilterControl] & OutputDataRateMask;
                if(code >= OutputDataRates.Length)
                {
                    this.Log(LogLevel.Error, "FILTER_CTL selects the reserved ODR 0x{0:X}, sampling at 400 Hz", code);
                    return 400.0;
                }
                return OutputDataRates[code];
            }
        }

        private int InactivityTime => registers[(int)Register.TimeInactLow] | (registers[(int)Register.TimeInactHigh] << 8);

        private struct Vector
        {
            public Vector(int x, int y, int z)
            {
                X = x;
                Y = y;
                Z = z;
            }

            public readonly int X;
            public readonly int Y;
            public readonly int Z;
        }

        private enum Command : byte
        {
            None = 0x00,
            WriteRegister = 0x0A,
            ReadRegister = 0x0B,
            ReadFifo = 0x0D,
        }

        private enum Register : byte
        {
            DeviceIdAd = 0x00,
            DeviceIdMst = 0x01,
            PartId = 0x02,
            RevId = 0x03,
            Status = 0x0B,
            FifoEntriesLow = 0x0C,
            FifoEntriesHigh = 0x0D,
            XDataHigh = 0x0E,
            XDataLow = 0x0F,
            YDataHigh = 0x10,
            YDataLow = 0x11,
            ZDataHigh = 0x12,
            ZDataLow = 0x13,
            TempHigh = 0x14,
            TempLow = 0x15,
            AdcHigh = 0x16,
            AdcLow = 0x17,
            SoftReset = 0x1F,
            ThreshActHigh = 0x20,
            ThreshActLow = 0x21,
            TimeAct = 0x22,
            ThreshInactHigh = 0x23,
            ThreshInactLow = 0x24,
            TimeInactLow = 0x25,
            TimeInactHigh = 0x26,
            ActInactControl = 0x27,
            FifoControl = 0x28,
            FifoSamples = 0x29,
            IntMap1 = 0x2A,
            IntMap2 = 0x2B,
            FilterControl = 0x2C,
            PowerControl = 0x2D,
            SelfTest = 0x2E,
        }

        private enum StatusBit
        {
            DataReady = 0,
            FifoReady = 1,
            FifoWatermark = 2,
            FifoOverrun = 3,
            Activity = 4,
            Inactivity = 5,
            Awake = 6,
            UserRegisterError = 7,
        }

        private enum FifoMode
        {
            Disabled = 0,
            OldestSaved = 1,
            Stream = 2,
            Triggered = 3,
        }

        private enum FifoReadMode
        {
            FourteenBit,
            EightBit,
        }

        private enum AxisTag
        {
            X = 0,
            Y = 1,
            Z = 2,
            Temperature = 3,
        }

        private Command command;
        private int frameIndex;
        private byte address;
        private bool dataReady;
        private bool awake;
        private int inactiveSamples;
        private byte? pendingLowByte;
        private Vector? activeReference;
        private readonly byte[] registers;
        private readonly Queue<ushort> fifo;
        private readonly Queue<Vector> gesture = new Queue<Vector>();
        private readonly HashSet<byte> unknownRegisters;
        private readonly LimitTimer sampleTimer;

        private const int RegisterCount = 0x80;
        private const int FifoDepth = 512;
        private const int FullScaleCodes = 8192;
        private const long TimerFrequency = 1000000;
        private const byte DeviceIdAd = 0xAD;
        private const byte DeviceIdMst = 0x1D;
        private const byte PartId = 0xF7;
        private const byte RevId = 0x03;
        private const byte DefaultFilterControl = 0x13;
        private const byte SoftResetKey = 0x52;
        private const byte PowerControlModeMask = 0x03;
        private const byte PowerControlMeasure = 0x02;
        private const byte FifoModeMask = 0x03;
        private const byte FifoSamplesHighBit = 1 << 2;
        private const byte FifoEightBitRead = 1 << 5;
        private const byte RangeMask = 0xC0;
        private const int RangeShift = 6;
        private const byte OutputDataRateMask = 0x07;
        private const byte ActivityEnable = 1 << 0;
        private const byte InactivityEnable = 1 << 2;
        private const byte InterruptSourceMask = 0x7F;
        private const byte InterruptActiveLow = 0x80;
        private const int StickyStatusBits = (1 << (int)StatusBit.Activity) | (1 << (int)StatusBit.Inactivity);
        private const int LiveStatusBits = (1 << (int)StatusBit.DataReady) | (1 << (int)StatusBit.FifoReady)
            | (1 << (int)StatusBit.FifoWatermark) | (1 << (int)StatusBit.Awake);
        private static readonly double[] OutputDataRates = { 12.5, 25.0, 50.0, 100.0, 200.0, 400.0 };
    }
}
