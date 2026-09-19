//
// TI ADS1115-class heat-flux ADC on TWI0 at 0x49, the part the firmware calls
// "SN19020X6". The driver at 0x5b20c writes CONFIG big-endian (0x0AE3 by
// default: AIN0-AIN1 differential, PGA +-0.256 V, single shot), polls the OS bit
// until the conversion is done and reads CONVERSION. It reconstructs microvolts
// as raw * full_scale_mV * 1000 >> 15 at 0x5b384, so the count here is the exact
// inverse of that. There is no id register to answer.
//
using System;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.I2C;

namespace Antmicro.Renode.Peripherals.Sensors
{
    public class ADS1115 : II2CPeripheral
    {
        public ADS1115()
        {
            Reset();
        }

        // Input, set from the run script: the differential AIN0-AIN1 voltage the
        // thermopile puts out. Kept out of Reset() so it survives the SYSRESETREQ
        // the app's fault handler issues. The default is a resting wrist, a small
        // positive heat flux out of the skin.
        public double InputMicrovolts { get; set; } = 300.0;

        public void Write(byte[] data)
        {
            if(data.Length == 0)
            {
                this.Log(LogLevel.Warning, "empty I2C write");
                return;
            }
            addressed = true;
            pointer = (Register)data[0];
            streamOffset = 0;
            if(data.Length == 1)
            {
                return;
            }
            if(data.Length != 3)
            {
                this.Log(LogLevel.Warning, "write of {0} data bytes to {1}, registers are 16-bit", data.Length - 1, pointer);
                return;
            }
            WriteRegister(pointer, (ushort)((data[1] << 8) | data[2]));
        }

        public byte[] Read(int count = 1)
        {
            var result = new byte[count];
            if(!addressed)
            {
                // The controller model clocks a byte in before the register
                // address has been written; the part has nothing to answer there.
                return result;
            }
            for(var i = 0; i < count; i++)
            {
                result[i] = NextByte();
            }
            return result;
        }

        public void FinishTransmission()
        {
            addressed = false;
            streamOffset = 0;
        }

        public void Reset()
        {
            config = ConfigResetValue;
            loThreshold = LoThresholdResetValue;
            hiThreshold = HiThresholdResetValue;
            streamOffset = 0;
            pointer = Register.Conversion;
            addressed = false;
        }

        // The controller clocks the bytes of a burst out one Read() call at a
        // time, so where the burst has got to has to live here: registers are
        // 16-bit and shifted out MSB first, the pointer advancing once per word.
        private byte NextByte()
        {
            var offset = streamOffset;
            streamOffset++;
            var word = ReadRegister((Register)((byte)pointer + offset / 2));
            return (offset % 2) == 0 ? (byte)(word >> 8) : (byte)(word & 0xFF);
        }

        private ushort ReadRegister(Register register)
        {
            switch(register)
            {
            case Register.Conversion:
                return (ushort)Quantize(InputMicrovolts);
            case Register.Config:
                // Single-shot conversions complete within the write that starts
                // them, so the part is never busy and OS always reads back 1.
                return (ushort)(config | OperationalStatusBit);
            case Register.LoThresh:
                return loThreshold;
            case Register.HiThresh:
                return hiThreshold;
            }
            this.Log(LogLevel.Warning, "read from unknown register 0x{0:X2}", (byte)register);
            return 0;
        }

        private void WriteRegister(Register register, ushort value)
        {
            switch(register)
            {
            case Register.Config:
                config = (ushort)(value & ~OperationalStatusBit);
                return;
            case Register.LoThresh:
                loThreshold = value;
                return;
            case Register.HiThresh:
                hiThreshold = value;
                return;
            case Register.Conversion:
                this.Log(LogLevel.Warning, "write 0x{0:X4} to read-only {1}", value, register);
                return;
            }
            this.Log(LogLevel.Warning, "write 0x{0:X4} to unknown register 0x{1:X2}", value, (byte)register);
        }

        private short Quantize(double microvolts)
        {
            var fullScaleMicrovolts = (double)FullScaleMillivolts[(config >> PgaShift) & PgaMask] * 1000;
            var counts = Math.Round(microvolts * FullScaleCounts / fullScaleMicrovolts);
            if(counts > short.MaxValue)
            {
                return short.MaxValue;
            }
            if(counts < short.MinValue)
            {
                return short.MinValue;
            }
            return (short)counts;
        }

        private enum Register : byte
        {
            Conversion = 0x00,
            Config = 0x01,
            LoThresh = 0x02,
            HiThresh = 0x03,
        }

        // Datasheet PGA table; codes 5..7 all select the +-0.256 V range.
        private static readonly int[] FullScaleMillivolts = { 6144, 4096, 2048, 1024, 512, 256, 256, 256 };
        private const double FullScaleCounts = 32768.0;
        private const int PgaShift = 9;
        private const int PgaMask = 0x7;
        private const int OperationalStatusBit = 1 << 15;
        private const ushort ConfigResetValue = 0x8583;
        private const ushort LoThresholdResetValue = 0x8000;
        private const ushort HiThresholdResetValue = 0x7FFF;

        private bool addressed;
        private int streamOffset;
        private Register pointer;
        private ushort config;
        private ushort loThreshold;
        private ushort hiThreshold;
    }
}
