//
// TI TMP117 skin temperature sensor on TWI0 at 0x48. The driver at 0x5e450
// writes CONFIGURATION big-endian, waits for the conversion, then reads
// TEMP_RESULT and splits it as Q7 ("[TMP117] temp=%d.%d deg C (Q7=0x%04x)");
// it never reads DEVICE_ID, so the id is here only for completeness.
//
using System;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.I2C;

namespace Antmicro.Renode.Peripherals.Sensors
{
    public class TMP117 : II2CPeripheral
    {
        public TMP117()
        {
            Reset();
        }

        // Input, set from the run script. Kept out of Reset() so it survives the
        // SYSRESETREQ the app's fault handler issues. The default is a watch worn
        // on the wrist: skin, not room, temperature.
        public double TemperatureCelsius { get; set; } = 33.0;

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
            configuration = ConfigurationResetValue;
            highLimit = HighLimitResetValue;
            lowLimit = LowLimitResetValue;
            eepromUnlock = 0;
            temperatureOffset = 0;
            conversionAvailable = false;
            streamOffset = 0;
            pointer = Register.TempResult;
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
            case Register.TempResult:
                conversionAvailable = false;
                return (ushort)Quantize(TemperatureCelsius);
            case Register.Configuration:
                return (ushort)(conversionAvailable ? configuration | DataReadyBit : configuration & ~DataReadyBit);
            case Register.THigh:
                return highLimit;
            case Register.TLow:
                return lowLimit;
            case Register.EepromUnlock:
                return eepromUnlock;
            case Register.TemperatureOffset:
                return temperatureOffset;
            case Register.DeviceId:
                return DeviceIdValue;
            }
            this.Log(LogLevel.Warning, "read from unknown register 0x{0:X2}", (byte)register);
            return 0;
        }

        private void WriteRegister(Register register, ushort value)
        {
            switch(register)
            {
            case Register.Configuration:
                // A mode that converts restarts conversion here; the sim has no
                // conversion time, so the result is ready by the driver's poll.
                // The firmware also parks the part in shutdown, which converts
                // nothing and must leave DATA_READY clear.
                configuration = (ushort)(value & ~DataReadyBit);
                conversionAvailable = ((value >> ModeShift) & ModeMask) != (int)ConversionMode.Shutdown;
                return;
            case Register.THigh:
                highLimit = value;
                return;
            case Register.TLow:
                lowLimit = value;
                return;
            case Register.EepromUnlock:
                eepromUnlock = value;
                return;
            case Register.TemperatureOffset:
                temperatureOffset = value;
                return;
            case Register.TempResult:
            case Register.DeviceId:
                this.Log(LogLevel.Warning, "write 0x{0:X4} to read-only {1}", value, register);
                return;
            }
            this.Log(LogLevel.Warning, "write 0x{0:X4} to unknown register 0x{1:X2}", value, (byte)register);
        }

        private short Quantize(double celsius)
        {
            var counts = Math.Round(celsius / DegreesPerCount);
            if(counts > short.MaxValue || counts < short.MinValue)
            {
                throw new ArgumentOutOfRangeException(nameof(celsius), "outside the TMP117's -256..256 deg C range");
            }
            return (short)counts;
        }

        private enum ConversionMode
        {
            Continuous = 0,
            Shutdown = 1,
            ContinuousAlias = 2,
            OneShot = 3,
        }

        private enum Register : byte
        {
            TempResult = 0x00,
            Configuration = 0x01,
            THigh = 0x02,
            TLow = 0x03,
            EepromUnlock = 0x04,
            TemperatureOffset = 0x07,
            DeviceId = 0x0F,
        }

        private const double DegreesPerCount = 1.0 / 128;
        private const ushort ConfigurationResetValue = 0x0220;
        private const ushort HighLimitResetValue = 0x6000;
        private const ushort LowLimitResetValue = 0x8000;
        private const ushort DeviceIdValue = 0x0117;
        private const int DataReadyBit = 1 << 13;
        private const int ModeShift = 10;
        private const int ModeMask = 0x3;

        private bool addressed;
        private int streamOffset;
        private bool conversionAvailable;
        private Register pointer;
        private ushort configuration;
        private ushort highLimit;
        private ushort lowLimit;
        private ushort eepromUnlock;
        private ushort temperatureOffset;
    }
}
