//
// Digital crown: a PixArt PAT9125-family optical motion sensor on TWI0 at 0x79.
// The firmware's driver ("[DIGITAL CROWN]") gates on product ids 0x31/0x92,
// runs the PAT9125 power-up sequence (bank 0x7F, soft reset 0x06=0x97 then
// 0x06=0x17, write-protect 0x09, resolutions 0x0D/0x0E, orientation 0x19) and
// reads motion as the 12-bit Delta_X/Delta_Y pair split over 0x03/0x04/0x12.
// Rotation reaches the UI as Delta_X only, and the driver negates the raw
// value, so Rotate(+1) has to put -1 on the wire.
//
using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.I2C;

namespace Antmicro.Renode.Peripherals.Sensors
{
    public class Crown : II2CPeripheral, INumberedGPIOOutput
    {
        public Crown(IMachine machine)
        {
            this.machine = machine;
            registers = new Dictionary<Reg, byte>();
            Motion = new GPIO();
            Connections = new Dictionary<int, IGPIO> { { 0, Motion } };
            Reset();
        }

        // The MOTION pin is open-drain with a pull-up on the board: idle high,
        // pulled low while an unread motion report is pending.
        public GPIO Motion { get; private set; }
        public IReadOnlyDictionary<int, IGPIO> Connections { get; private set; }

        // Turning the crown one detent at a time. Sign selects the direction the
        // UI sees; counts accumulate until the driver reads the delta registers.
        public void Rotate(int steps)
        {
            if(steps == 0)
            {
                throw new RecoverableException("steps must be non-zero");
            }
            rawDeltaX = Saturate(rawDeltaX - steps);
            motionPending = true;
            Motion.Set(false);
        }

        public void Write(byte[] data)
        {
            if(data.Length == 0)
            {
                this.Log(LogLevel.Warning, "empty I2C write");
                return;
            }
            selected = (Reg)data[0];
            for(int i = 1; i < data.Length; i++)
            {
                WriteRegister((Reg)((byte)selected + i - 1), data[i]);
            }
        }

        public byte[] Read(int count = 1)
        {
            var result = new byte[count];
            for(int i = 0; i < count; i++)
            {
                result[i] = ReadRegister((Reg)((byte)selected + i));
            }
            return result;
        }

        public void FinishTransmission()
        {
            // The register pointer survives the repeated start between the
            // address phase and the data phase, so there is nothing to drop here.
        }

        public void Reset()
        {
            registers.Clear();
            registers[Reg.ProductId1] = ProductId1Value;
            registers[Reg.ProductId2] = ProductId2Value;
            registers[Reg.FrameAverage] = FrameAveragePowerOn;
            selected = Reg.ProductId1;
            rawDeltaX = 0;
            rawDeltaY = 0;
            motionPending = false;
            Motion.Set(true);
        }

        private byte ReadRegister(Reg register)
        {
            switch(register)
            {
            case Reg.MotionStatus:
                return motionPending ? MotionStatusBit : (byte)0;
            case Reg.DeltaXLow:
                return (byte)(rawDeltaX & 0xFF);
            case Reg.DeltaYLow:
                return (byte)(rawDeltaY & 0xFF);
            case Reg.DeltaXYHigh:
                // Last register of a motion burst: the report is consumed here.
                var high = (byte)((((rawDeltaX >> 8) & 0xF) << 4) | ((rawDeltaY >> 8) & 0xF));
                ConsumeMotion();
                return high;
            }
            byte value;
            if(registers.TryGetValue(register, out value))
            {
                return value;
            }
            if(!IsKnown(register))
            {
                this.Log(LogLevel.Warning, "read from unknown register 0x{0:X2}", (byte)register);
                return 0;
            }
            return 0;
        }

        private void WriteRegister(Reg register, byte value)
        {
            if(!IsKnown(register))
            {
                this.Log(LogLevel.Warning, "write 0x{0:X2} to unknown register 0x{1:X2}", value, (byte)register);
                return;
            }
            if(register == Reg.ProductId1 || register == Reg.ProductId2)
            {
                this.Log(LogLevel.Warning, "write 0x{0:X2} to read-only product id 0x{1:X2}", value, (byte)register);
                return;
            }
            if(register == Reg.Configuration && (value & ConfigurationResetBit) != 0)
            {
                Reset();
                return;
            }
            registers[register] = value;
        }

        private void ConsumeMotion()
        {
            rawDeltaX = 0;
            rawDeltaY = 0;
            motionPending = false;
            Motion.Set(true);
        }

        private int Saturate(int delta)
        {
            if(delta > DeltaMax)
            {
                return DeltaMax;
            }
            if(delta < DeltaMin)
            {
                return DeltaMin;
            }
            return delta;
        }

        private bool IsKnown(Reg register)
        {
            return Enum.IsDefined(typeof(Reg), register);
        }

        private enum Reg : byte
        {
            ProductId1 = 0x00,
            ProductId2 = 0x01,
            MotionStatus = 0x02,
            DeltaXLow = 0x03,
            DeltaYLow = 0x04,
            OperationMode = 0x05,
            Configuration = 0x06,
            WriteProtect = 0x09,
            Sleep1 = 0x0A,
            Sleep2 = 0x0B,
            ResolutionX = 0x0D,
            ResolutionY = 0x0E,
            DeltaXYHigh = 0x12,
            Shutter = 0x14,
            FrameAverage = 0x17,
            Orientation = 0x19,
            TuningA = 0x2B,
            TuningB = 0x2D,
            TuningC = 0x7C,
            BankSelect = 0x7F,
        }

        private const byte ProductId1Value = 0x31;
        private const byte ProductId2Value = 0x92;
        private const byte FrameAveragePowerOn = 0x80;
        private const byte MotionStatusBit = 0x80;
        private const byte ConfigurationResetBit = 0x80;
        private const int DeltaMax = 2047;
        private const int DeltaMin = -2048;

        private Reg selected;
        private int rawDeltaX;
        private int rawDeltaY;
        private bool motionPending;
        private readonly Dictionary<Reg, byte> registers;
        private readonly IMachine machine;
    }
}
