//
// Maxim MAX86173 optical (PPG) front end, on SPIM1 with chip select P0.26,
// supply enable P1.12 and an open-drain active-low interrupt on P1.14. It is
// the input for heart rate, worn detection, SpO2 and the passive-HR event.
//
// Framing, from the driver's register helpers at 0x53284 (read) and 0x5333c
// (write): {register, 0x80, data..} reads and {register, 0x00, data} writes,
// one frame per EasyDMA transfer, the address auto-incrementing across the
// data bytes except on the FIFO data register, which streams samples.
//
// The register map is the MAX86171 family one, established from the driver
// rather than from the datasheet: 0x00..0x03 status, 0x06/0x07 the FIFO
// counters (0x06 bit 7 is the count's 9th bit, bits 6:0 the overflow count),
// 0x08 FIFO data, 0x09 the almost-full threshold, 0x0A FIFO configuration
// (bit 4 flushes), 0x0C system control (bit 0 reset, bit 1 shutdown), 0x0D the
// per-measurement enable bitmap, 0x0E system configuration, 0x15..0x17 the
// frame-rate clock select and divider, 0x18 + 8*(n-1) the MEAS<n> block, 0x78
// the interrupt enables and 0xFF the part id, which must read 0x48.
//
// A part that answers the part id but delivers no samples is worse than an
// absent one: the driver arms a 10 s software watchdog once it has started, so
// the FIFO here really runs, and the interrupt is asserted when the FIFO
// reaches the threshold the driver programmed.
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
    public class Max86173 : ISPIPeripheral, INumberedGPIOOutput
    {
        public Max86173(IMachine machine)
        {
            registers = new byte[RegisterCount];
            fifo = new Queue<uint>();
            unknownRegisters = new HashSet<byte>();
            Interrupt = new GPIO();
            Connections = new Dictionary<int, IGPIO> { { 0, Interrupt } };
            frameTimer = new LimitTimer(machine.ClockSource, TimerFrequency, this, "max86173-frame", limit: 1,
                direction: Direction.Ascending, enabled: false, workMode: WorkMode.Periodic, eventEnabled: true);
            frameTimer.LimitReached += PushFrame;
            LoadDefaults();
        }

        // Open drain with a pull-up on the board: idle high, pulled low while an
        // enabled interrupt condition stands. The firmware senses it low.
        public GPIO Interrupt { get; private set; }
        public IReadOnlyDictionary<int, IGPIO> Connections { get; private set; }

        // Inputs, settable from the run script and kept out of Reset() so they
        // survive the SYSRESETREQ the app's fault handler issues.
        public int HeartRateBpm
        {
            get => heartRateBpm;
            set
            {
                if(value < 20 || value > 250)
                {
                    throw new RecoverableException("heart rate must be between 20 and 250 bpm");
                }
                heartRateBpm = value;
            }
        }

        // The pulsatile part of the signal, in ADC counts, and the perfusion
        // level the worn detector keys on: skin returns light, air does not.
        public int PulseAmplitude { get; set; } = 6000;
        public bool Worn { get; set; } = true;

        public byte Transmit(byte data)
        {
            var index = frameIndex;
            frameIndex++;
            if(index == 0)
            {
                address = data;
                return 0;
            }
            if(index == 1)
            {
                reading = data == ReadCommand;
                if(!reading && data != WriteCommand)
                {
                    this.Log(LogLevel.Warning, "frame for register 0x{0:X2} carries direction byte 0x{1:X2}, which is neither a read nor a write", address, data);
                }
                return 0;
            }
            if(reading)
            {
                return ReadRegister();
            }
            WriteRegister(data);
            return 0;
        }

        public void FinishTransmission()
        {
            frameIndex = 0;
        }

        public void Reset()
        {
            LoadDefaults();
        }

        private byte ReadRegister()
        {
            if(address == (byte)Register.FifoData)
            {
                return NextFifoByte();
            }
            var value = registers[address];
            if(address == (byte)Register.Status1)
            {
                // Status bits are cleared by the read that reports them, and the
                // interrupt line follows, which is how the driver's handler
                // rearms; the clear happens on the first byte of the burst.
                registers[(byte)Register.Status1] = 0;
                registers[(byte)Register.Status2] = 0;
                registers[(byte)Register.Status3] = 0;
                UpdateInterrupt();
            }
            Advance();
            return value;
        }

        private byte NextFifoByte()
        {
            if(fifoByteIndex == 0)
            {
                if(fifo.Count == 0)
                {
                    this.Log(LogLevel.Warning, "read from an empty FIFO");
                    fifoSample = 0;
                }
                else
                {
                    fifoSample = fifo.Dequeue();
                }
            }
            var shift = 16 - 8 * fifoByteIndex;
            fifoByteIndex = (fifoByteIndex + 1) % BytesPerSample;
            if(fifoByteIndex == 0)
            {
                UpdateFifoCount();
            }
            return (byte)(fifoSample >> shift);
        }

        private void WriteRegister(byte value)
        {
            switch((Register)address)
            {
            case Register.SystemControl:
                registers[address] = (byte)(value & ~ResetBit);
                if((value & ResetBit) != 0)
                {
                    LoadDefaults();
                    return;
                }
                UpdateSampling();
                return;
            case Register.FifoConfig2:
                registers[address] = (byte)(value & ~FlushFifoBit);
                if((value & FlushFifoBit) != 0)
                {
                    fifo.Clear();
                    fifoByteIndex = 0;
                    overflowCount = 0;
                    UpdateFifoCount();
                    UpdateInterrupt();
                }
                return;
            case Register.MeasurementEnable:
                registers[address] = value;
                UpdateSampling();
                return;
            case Register.InterruptEnable1:
                registers[address] = value;
                UpdateInterrupt();
                return;
            case Register.PartId:
                this.Log(LogLevel.Warning, "write 0x{0:X2} to the read-only part id", value);
                return;
            }
            if(!Enum.IsDefined(typeof(Register), (Register)address) && !IsMeasurementRegister(address)
               && unknownRegisters.Add(address))
            {
                this.Log(LogLevel.Warning, "write 0x{0:X2} to unmodelled register 0x{1:X2}", value, address);
            }
            registers[address] = value;
            if(address == (byte)Register.FrameClockSelect || address == (byte)Register.FrameDividerHigh
               || address == (byte)Register.FrameDividerLow)
            {
                UpdateSampling();
            }
            Advance();
        }

        private void Advance()
        {
            address = (byte)(address + 1);
        }

        private void UpdateSampling()
        {
            var shutdown = (registers[(byte)Register.SystemControl] & ShutdownBit) != 0;
            var divider = (registers[(byte)Register.FrameDividerHigh] << 8) | registers[(byte)Register.FrameDividerLow];
            var running = !shutdown && EnabledMeasurements.Count > 0 && divider > 0;
            if(!running)
            {
                frameTimer.Enabled = false;
                return;
            }
            var clock = (registers[(byte)Register.FrameClockSelect] & FrameClock32768Bit) != 0
                ? FrameClock32768Hz : FrameClock32000Hz;
            var rate = (double)clock / divider;
            frameTimer.Limit = (ulong)Math.Max(1, Math.Round(TimerFrequency / rate));
            frameTimer.Enabled = true;
            this.Log(LogLevel.Info, "sampling measurements {0} at {1:F2} Hz",
                string.Join(", ", EnabledMeasurements), rate);
        }

        private List<int> EnabledMeasurements
        {
            get
            {
                var enabled = new List<int>();
                var bitmap = registers[(byte)Register.MeasurementEnable];
                for(var measurement = 1; measurement <= MeasurementCount; measurement++)
                {
                    if((bitmap & (1 << (measurement - 1))) != 0)
                    {
                        enabled.Add(measurement);
                    }
                }
                return enabled;
            }
        }

        private void PushFrame()
        {
            var seconds = frameCounter / Math.Max(1.0, currentRate);
            frameCounter++;
            foreach(var measurement in EnabledMeasurements)
            {
                foreach(var channel in Channels(measurement))
                {
                    Push(channel, Synthesise(seconds, channel));
                }
            }
            UpdateFifoCount();
            UpdateInterrupt();
        }

        // One sample per photodiode of each enabled measurement, tagged the way
        // the silicon tags them: MEAS<n> photodiode 1 is 2n-1 and photodiode 2
        // is 2n, which is the order the driver registers as it programs the
        // measurements, and it faults on a sample that arrives out of that order.
        private IEnumerable<int> Channels(int measurement)
        {
            yield return 2 * measurement - 1;
            if(SecondPhotodiodeEnabled(measurement))
            {
                yield return 2 * measurement;
            }
        }

        private bool SecondPhotodiodeEnabled(int measurement)
        {
            var configuration = registers[MeasurementRegister(measurement, MeasurementConfig2Offset)];
            return (configuration & SecondPhotodiodeBit) != 0;
        }

        private uint Synthesise(double seconds, int channel)
        {
            var baseline = Worn ? WornBaseline : UnwornBaseline;
            // Each photodiode sees its own share of the returned light, so the
            // channels differ; the algorithm needs them to move together in time.
            var scale = 1.0 - 0.15 * ((channel - 1) % 4);
            var pulse = Worn ? PulseAmplitude * scale * Math.Sin(2 * Math.PI * heartRateBpm * seconds / 60.0) : 0;
            var value = (int)Math.Round(baseline * scale + pulse);
            return (uint)Math.Min(Math.Max(value, 0), MaxSampleValue);
        }

        private void Push(int channel, uint value)
        {
            if(fifo.Count >= FifoDepth)
            {
                overflowCount = Math.Min(overflowCount + 1, OverflowCountMax);
                if((registers[(byte)Register.FifoConfig2] & RollOverBit) == 0)
                {
                    return;
                }
                fifo.Dequeue();
            }
            fifo.Enqueue((uint)((channel & 0xF) << 20 | (int)(value & 0xFFFFF)));
        }

        private void UpdateFifoCount()
        {
            var count = Math.Min(fifo.Count, FifoDepth);
            registers[(byte)Register.FifoCount1] = (byte)((count >> 8) << 7 | overflowCount);
            registers[(byte)Register.FifoCount2] = (byte)count;
            if(count >= AlmostFullLevel)
            {
                registers[(byte)Register.Status1] |= AlmostFullBit;
            }
        }

        // The threshold register counts the free slots left, so the interrupt is
        // due once the FIFO holds everything but that many samples.
        private int AlmostFullLevel => FifoDepth - registers[(byte)Register.FifoAlmostFull];

        private void UpdateInterrupt()
        {
            var enabled = registers[(byte)Register.InterruptEnable1];
            var pending = (registers[(byte)Register.Status1] & enabled & (AlmostFullBit | FrameReadyBit)) != 0;
            Interrupt.Set(!pending);
        }

        private void LoadDefaults()
        {
            Array.Clear(registers, 0, registers.Length);
            registers[(byte)Register.PartId] = PartIdValue;
            registers[(byte)Register.FrameClockSelect] = FrameClock32768Bit;
            registers[(byte)Register.Status1] = PowerReadyBit;
            fifo.Clear();
            fifoByteIndex = 0;
            fifoSample = 0;
            overflowCount = 0;
            frameCounter = 0;
            frameIndex = 0;
            address = 0;
            reading = false;
            frameTimer.Enabled = false;
            Interrupt.Set(true);
        }

        private double currentRate => frameTimer.Limit == 0 ? 1.0 : (double)TimerFrequency / frameTimer.Limit;

        private static bool IsMeasurementRegister(byte register)
        {
            return register >= MeasurementBase && register < MeasurementBase + MeasurementCount * MeasurementStride;
        }

        private static byte MeasurementRegister(int measurement, int offset)
        {
            return (byte)(MeasurementBase + (measurement - 1) * MeasurementStride + offset);
        }

        private enum Register : byte
        {
            Status1 = 0x00,
            Status2 = 0x01,
            Status3 = 0x02,
            Status4 = 0x03,
            FifoWritePointer = 0x04,
            FifoReadPointer = 0x05,
            FifoCount1 = 0x06,
            FifoCount2 = 0x07,
            FifoData = 0x08,
            FifoAlmostFull = 0x09,
            FifoConfig2 = 0x0A,
            SystemControl = 0x0C,
            MeasurementEnable = 0x0D,
            SystemConfig = 0x0E,
            PhotodiodeBias = 0x0F,
            PinConfig = 0x10,
            FrameClockSelect = 0x15,
            FrameDividerHigh = 0x16,
            FrameDividerLow = 0x17,
            InterruptEnable1 = 0x78,
            InterruptEnable2 = 0x79,
            PartId = 0xFF,
        }

        private const byte PartIdValue = 0x48;
        private const byte ReadCommand = 0x80;
        private const byte WriteCommand = 0x00;
        private const int RegisterCount = 0x100;
        private const int FifoDepth = 256;
        private const int BytesPerSample = 3;
        private const int MaxSampleValue = 0x7FFFF;
        private const int MeasurementCount = 8;
        private const byte MeasurementBase = 0x18;
        private const int MeasurementStride = 8;
        private const int MeasurementConfig2Offset = 2;
        private const byte SecondPhotodiodeBit = 1 << 3;
        private const byte PowerReadyBit = 1 << 0;
        private const byte FrameReadyBit = 1 << 6;
        private const byte AlmostFullBit = 1 << 7;
        private const byte ResetBit = 1 << 0;
        private const byte ShutdownBit = 1 << 1;
        private const byte RollOverBit = 1 << 1;
        private const byte FlushFifoBit = 1 << 4;
        private const byte FrameClock32768Bit = 1 << 0;
        private const int FrameClock32768Hz = 32768;
        private const int FrameClock32000Hz = 32000;
        private const int OverflowCountMax = 0x7F;
        private const int WornBaseline = 0x28000;
        private const int UnwornBaseline = 0x400;
        private const long TimerFrequency = 32768;

        private int heartRateBpm = 70;
        private byte address;
        private bool reading;
        private int frameIndex;
        private int fifoByteIndex;
        private uint fifoSample;
        private int overflowCount;
        private long frameCounter;
        private readonly byte[] registers;
        private readonly Queue<uint> fifo;
        private readonly HashSet<byte> unknownRegisters;
        private readonly LimitTimer frameTimer;
    }
}
