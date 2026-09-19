//
// Broadcom APDS-9306 ambient light sensor on TWI0 at 0x52. The driver enables it
// at 0x508ec (MAIN_CTRL, then LS_GAIN=0x04 and LS_MEAS_RATE=0x32) and reads at
// 0x50960: one MAIN_STATUS byte, and only if LS_DATA_STATUS is set, three bytes
// from LS_DATA_0. It reads PART_ID at 0x509c8 but never compares it. There is no
// retry around the status poll, so a sample has to be waiting whenever the
// driver looks, which is why the pending flag re-arms on the measurement rate.
//
using System;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.I2C;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Sensors
{
    public class APDS9306 : II2CPeripheral
    {
        public APDS9306(IMachine machine)
        {
            this.machine = machine;
            Reset();
        }

        // Input, set from the run script. Kept out of Reset() so it survives the
        // SYSRESETREQ the app's fault handler issues. The default is indoor light.
        public double Lux { get; set; } = 200.0;

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
            for(var i = 1; i < data.Length; i++)
            {
                WriteRegister((Register)((byte)pointer + i - 1), data[i]);
            }
        }

        public byte[] Read(int count = 1)
        {
            var result = new byte[count];
            if(!addressed)
            {
                // The controller model clocks a byte in before the register
                // address has been written; the part has nothing to answer there,
                // and answering would consume the sample the driver is about to
                // read.
                return result;
            }
            for(var i = 0; i < count; i++)
            {
                // The controller clocks the bytes of a burst out one Read() call
                // at a time, so where the burst has got to has to live here.
                result[i] = ReadRegister((Register)((byte)pointer + streamOffset));
                streamOffset++;
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
            mainControl = 0x00;
            measurementRate = MeasurementRateResetValue;
            gain = GainResetValue;
            interruptConfig = InterruptConfigResetValue;
            interruptPersistence = 0x00;
            thresholdUpper = ThresholdUpperResetValue;
            thresholdLower = 0;
            thresholdVariance = 0x00;
            powerOnStatus = true;
            samplePending = false;
            streamOffset = 0;
            pointer = Register.MainControl;
            addressed = false;
        }

        private byte ReadRegister(Register register)
        {
            switch(register)
            {
            case Register.MainControl:
                return mainControl;
            case Register.MeasurementRate:
                return measurementRate;
            case Register.Gain:
                return gain;
            case Register.PartId:
                return PartIdValue;
            case Register.MainStatus:
                var status = (byte)((powerOnStatus ? PowerOnStatusBit : 0) | (samplePending ? DataStatusBit : 0));
                powerOnStatus = false;
                return status;
            case Register.LightData0:
                ConsumeSample();
                return (byte)(Counts() & 0xFF);
            case Register.LightData1:
                return (byte)((Counts() >> 8) & 0xFF);
            case Register.LightData2:
                return (byte)((Counts() >> 16) & 0x0F);
            case Register.InterruptConfig:
                return interruptConfig;
            case Register.InterruptPersistence:
                return interruptPersistence;
            case Register.ThresholdUpper0:
            case Register.ThresholdUpper1:
            case Register.ThresholdUpper2:
                return Byte(thresholdUpper, register - Register.ThresholdUpper0);
            case Register.ThresholdLower0:
            case Register.ThresholdLower1:
            case Register.ThresholdLower2:
                return Byte(thresholdLower, register - Register.ThresholdLower0);
            case Register.ThresholdVariance:
                return thresholdVariance;
            }
            this.Log(LogLevel.Warning, "read from unknown register 0x{0:X2}", (byte)register);
            return 0;
        }

        private void WriteRegister(Register register, byte value)
        {
            switch(register)
            {
            case Register.MainControl:
                if((value & SoftResetBit) != 0)
                {
                    Reset();
                    return;
                }
                mainControl = value;
                samplePending = (value & LightSensorEnableBit) != 0;
                return;
            case Register.MeasurementRate:
                if(((value >> ResolutionShift) & ResolutionMask) >= IntegrationTimeMilliseconds.Length
                    || (value & RateMask) >= MeasurementRateMilliseconds.Length)
                {
                    this.Log(LogLevel.Warning, "write 0x{0:X2} to {1} selects a reserved resolution or rate", value, register);
                    return;
                }
                measurementRate = value;
                return;
            case Register.Gain:
                if(value >= GainFactors.Length)
                {
                    this.Log(LogLevel.Warning, "write 0x{0:X2} to {1} selects a reserved gain", value, register);
                    return;
                }
                gain = value;
                return;
            case Register.InterruptConfig:
                interruptConfig = value;
                return;
            case Register.InterruptPersistence:
                interruptPersistence = value;
                return;
            case Register.ThresholdUpper0:
            case Register.ThresholdUpper1:
            case Register.ThresholdUpper2:
                thresholdUpper = Replace(thresholdUpper, register - Register.ThresholdUpper0, value);
                return;
            case Register.ThresholdLower0:
            case Register.ThresholdLower1:
            case Register.ThresholdLower2:
                thresholdLower = Replace(thresholdLower, register - Register.ThresholdLower0, value);
                return;
            case Register.ThresholdVariance:
                thresholdVariance = value;
                return;
            case Register.PartId:
            case Register.MainStatus:
                this.Log(LogLevel.Warning, "write 0x{0:X2} to read-only {1}", value, register);
                return;
            }
            this.Log(LogLevel.Warning, "write 0x{0:X2} to unknown register 0x{1:X2}", value, (byte)register);
        }

        private void ConsumeSample()
        {
            samplePending = false;
            if((mainControl & LightSensorEnableBit) == 0)
            {
                return;
            }
            machine.ScheduleAction(TimeInterval.FromMilliseconds(MeasurementRateMilliseconds[measurementRate & RateMask]),
                _ => samplePending = (mainControl & LightSensorEnableBit) != 0);
        }

        // Datasheet ALS resolution: 0.8 lux per count at gain 3 with a 100 ms
        // integration time, so one count at gain 1 and 100 ms is 2.4 lux, and both
        // gain and integration time scale the count linearly.
        private int Counts()
        {
            var resolutionCode = (measurementRate >> ResolutionShift) & ResolutionMask;
            var counts = Math.Round(Lux * GainFactors[gain]
                * (IntegrationTimeMilliseconds[resolutionCode] / ReferenceIntegrationTimeMilliseconds)
                / LuxPerCountAtUnitGain);
            return counts > MaxCounts ? MaxCounts : (int)counts;
        }

        private byte Byte(int value, int index)
        {
            return (byte)((value >> (8 * index)) & (index == 2 ? 0x0F : 0xFF));
        }

        private int Replace(int value, int index, byte replacement)
        {
            var mask = (index == 2 ? 0x0F : 0xFF) << (8 * index);
            return (value & ~mask) | ((replacement << (8 * index)) & mask);
        }

        private enum Register : byte
        {
            MainControl = 0x00,
            MeasurementRate = 0x04,
            Gain = 0x05,
            PartId = 0x06,
            MainStatus = 0x07,
            LightData0 = 0x0D,
            LightData1 = 0x0E,
            LightData2 = 0x0F,
            InterruptConfig = 0x19,
            InterruptPersistence = 0x1A,
            ThresholdUpper0 = 0x21,
            ThresholdUpper1 = 0x22,
            ThresholdUpper2 = 0x23,
            ThresholdLower0 = 0x24,
            ThresholdLower1 = 0x25,
            ThresholdLower2 = 0x26,
            ThresholdVariance = 0x27,
        }

        private static readonly int[] GainFactors = { 1, 3, 6, 9, 18 };
        private static readonly double[] IntegrationTimeMilliseconds = { 400, 200, 100, 50, 25, 3.125 };
        private static readonly ulong[] MeasurementRateMilliseconds = { 25, 50, 100, 200, 500, 1000, 2000 };
        private const double LuxPerCountAtUnitGain = 2.4;
        private const double ReferenceIntegrationTimeMilliseconds = 100;
        private const int MaxCounts = 0xFFFFF;
        private const byte PartIdValue = 0xB3;
        private const byte LightSensorEnableBit = 1 << 1;
        private const byte SoftResetBit = 1 << 4;
        private const byte DataStatusBit = 1 << 3;
        private const byte PowerOnStatusBit = 1 << 5;
        private const byte MeasurementRateResetValue = 0x22;
        private const byte GainResetValue = 0x01;
        private const byte InterruptConfigResetValue = 0x10;
        private const int ThresholdUpperResetValue = 0xFFFFF;
        private const int ResolutionShift = 4;
        private const int ResolutionMask = 0x7;
        private const int RateMask = 0x7;

        private bool addressed;
        private int streamOffset;
        private bool samplePending;
        private bool powerOnStatus;
        private Register pointer;
        private byte mainControl;
        private byte measurementRate;
        private byte gain;
        private byte interruptConfig;
        private byte interruptPersistence;
        private byte thresholdVariance;
        private int thresholdUpper;
        private int thresholdLower;
        private readonly IMachine machine;
    }
}
