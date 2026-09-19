//
// TE MS5849 barometer on TWI0 at 0x77, row 5 of the firmware's I2C device table
// at 0xb2350. Row 6 at 0x76 is an MS5837, and both drivers are in the image:
// the altimeter's hardware setup at 0x3dd58 reads configuration item 0xb, the
// hardware revision, and takes the MS5837 only when its low halfword is above
// 0x6f. The watch this simulation's flash was dumped from is below that, so the
// MS5849 at 0x77 is the part the firmware talks to and the only one modelled.
//
// Its command set, read out of the driver at 0x54e58..0x55160:
//   0x10             reset
//   0xE0 + 2k        read ROM word k, two bytes big-endian, sixteen words whose
//                    CRC-8 (polynomial 0x31, seeded zero) over all thirty-two
//                    bytes must come out zero
//   0x20, 0x22       write the two 16-bit configuration words, three bytes each
//   0x40 | channels  start a conversion, 0x04 pressure and 0x08 temperature
//   0x50 | channels  read the results, temperature's three bytes first
// The driver waits the OSR's conversion time plus a millisecond between the
// last two; conversions here finish inside the command that starts them.
//
using System;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.I2C;

namespace Antmicro.Renode.Peripherals.Sensors
{
    public class MS5849 : II2CPeripheral
    {
        public MS5849()
        {
            rom = BuildRom();
            Reset();
        }

        // Inputs, set from the run script. Kept out of Reset() so they survive the
        // SYSRESETREQ the app's fault handler issues. The default is a standard
        // atmosphere in a warm room.
        public double PressureMbar { get; set; } = 1013.25;
        public double TemperatureCelsius { get; set; } = 20.0;

        public void Write(byte[] data)
        {
            if(data.Length == 3 && (data[0] == ConfigWrite0 || data[0] == ConfigWrite1))
            {
                var index = (data[0] - ConfigWrite0) / 2;
                configuration[index] = (ushort)((data[1] << 8) | data[2]);
                this.Log(LogLevel.Info, "WRITE CONF {0} = 0x{1:X4}", index, configuration[index]);
                return;
            }
            if(data.Length != 1)
            {
                this.Log(LogLevel.Warning, "write of {0} bytes is neither a command nor a configuration word", data.Length);
                return;
            }
            Command(data[0]);
        }

        // The controller clocks a burst out one Read() call at a time, so how far
        // through the last command's answer the burst has got has to live here.
        // It also clocks a byte in before each command has been written, when
        // the last answer is spent and the part has nothing to say.
        public byte[] Read(int count = 1)
        {
            if(taken == stream.Length)
            {
                return new byte[count];
            }
            if(count > stream.Length - taken)
            {
                this.Log(LogLevel.Warning, "read of {0} bytes with {1} left of the last command's answer", count, stream.Length - taken);
                return new byte[count];
            }
            var result = new byte[count];
            Array.Copy(stream, taken, result, 0, count);
            taken += count;
            return result;
        }

        public void FinishTransmission()
        {
        }

        public void Reset()
        {
            configuration = new ushort[2];
            pressureCounts = 0;
            temperatureCounts = 0;
            stream = new byte[0];
            taken = 0;
        }

        private void Command(byte command)
        {
            taken = 0;
            if(command == ResetCommand)
            {
                this.Log(LogLevel.Info, "RESET");
                Reset();
                return;
            }
            if(command >= RomReadBase && (command & 1) == 0)
            {
                var word = (command - RomReadBase) / 2;
                this.Log(LogLevel.Info, "ROM READ {0} -> 0x{1:X4}", word, rom[word]);
                stream = new[] { (byte)(rom[word] >> 8), (byte)rom[word] };
                return;
            }
            var channels = command & ChannelMask;
            if((command & ~ChannelMask) == ConvertCommand && channels != 0)
            {
                Convert();
                this.Log(LogLevel.Info, "CONVERT{0}{1} -> pressure {2}, temperature {3}",
                    (channels & PressureChannel) != 0 ? " P" : "", (channels & TemperatureChannel) != 0 ? " T" : "",
                    pressureCounts, temperatureCounts);
                return;
            }
            if((command & ~ChannelMask) == AdcReadCommand && channels != 0)
            {
                stream = Results(channels);
                this.Log(LogLevel.Info, "ADC READ{0}{1} -> {2} bytes",
                    (channels & TemperatureChannel) != 0 ? " T" : "", (channels & PressureChannel) != 0 ? " P" : "",
                    stream.Length);
                return;
            }
            this.Log(LogLevel.Warning, "unknown command 0x{0:X2}", command);
        }

        // The driver takes the temperature channel's bytes off the wire first
        // and each channel's three bytes most significant first.
        private byte[] Results(int channels)
        {
            var result = new byte[3 * (((channels & TemperatureChannel) != 0 ? 1 : 0) + ((channels & PressureChannel) != 0 ? 1 : 0))];
            var at = 0;
            if((channels & TemperatureChannel) != 0)
            {
                at = Append(result, at, temperatureCounts);
            }
            if((channels & PressureChannel) != 0)
            {
                Append(result, at, pressureCounts);
            }
            return result;
        }

        private static int Append(byte[] buffer, int at, uint counts)
        {
            buffer[at] = (byte)(counts >> 16);
            buffer[at + 1] = (byte)(counts >> 8);
            buffer[at + 2] = (byte)counts;
            return at + 3;
        }

        // The compensation at 0x28790 is a polynomial in both raw counts at once,
        // so the counts that produce the wanted reading come out of it backwards:
        // temperature is monotonic in its own channel and pressure in its own,
        // and each depends weakly on the other, so alternating two bisections
        // settles on the pair within a count or two of exact.
        private void Convert()
        {
            var temperature = TemperatureCelsius * 100;
            var pressure = PressureMbar * 100;
            double t = CountsMidpoint;
            double p = CountsMidpoint;
            for(var round = 0; round < InversionRounds; round++)
            {
                t = Bisect(counts => Temperature(p, counts), temperature, Rising.Yes);
                p = Bisect(counts => Pressure(counts, t), pressure, Rising.No);
            }
            var reached = Pressure(p, t);
            if(Math.Abs(reached - pressure) > 1 || Math.Abs(Temperature(p, t) - temperature) > 1)
            {
                throw new ArgumentOutOfRangeException(nameof(PressureMbar),
                    $"{PressureMbar} mBar at {TemperatureCelsius} deg C is outside what this part's coefficients cover");
            }
            pressureCounts = (uint)Math.Round(p);
            temperatureCounts = (uint)Math.Round(t);
        }

        private enum Rising
        {
            No,
            Yes,
        }

        private static double Bisect(Func<double, double> f, double target, Rising direction)
        {
            var low = CountsLow;
            var high = CountsHigh;
            for(var step = 0; step < BisectionSteps; step++)
            {
                var middle = (low + high) / 2;
                if((f(middle) < target) == (direction == Rising.Yes))
                {
                    low = middle;
                }
                else
                {
                    high = middle;
                }
            }
            return (low + high) / 2;
        }

        // Both of these are the firmware's own arithmetic, transcribed from the
        // soft-float sequence at 0x28790: the coefficients scaled by powers of
        // two are the part's, the rest are constants the driver carries itself.
        private static double Temperature(double p, double t)
        {
            return 100.0 * (-C0 * 0.0625
                + C1 * 2.9103830456733704e-11 * p
                + C2 * 2.9802322387695312e-08 * t
                + p * p * -3.037801253e-14
                + t * t * -1.079036179e-10
                + t * t * t * 7.558105437e-18
                + t * t * t * t * -1.988019651e-25);
        }

        private static double Pressure(double p, double t)
        {
            return 100.0 * (-C4 * 4.656612873077393e-10 * p
                + C3 * 0.001953125
                + -C5 * 9.313225746154785e-10 * t
                + C6 * 8.881784197001252e-16 * p * p
                + t * t * 9.65965184878317e-10
                + C7 * 2.220446049250313e-16 * p * t
                + p * p * p * -7.44063838851138e-19
                + t * t * t * -6.86793865715534e-17
                + p * p * t * -2.73594458439415e-18
                + p * t * t * -1.93891892136926e-16
                + p * p * p * p * 1.11467352484525e-26
                + t * t * t * t * 1.83448924520177e-24
                + p * p * p * t * 1.23642876257138e-25
                + p * p * t * t * 6.19373870077221e-27
                + p * t * t * t * 1.37287101917619e-23
                + p * p * p * p * p * 8.11976718528928e-36
                + p * p * p * p * t * -9.35126613904527e-34
                + p * p * p * t * t * -1.73061923874059e-33
                + p * p * t * t * t * 2.92999600253747e-33
                + p * t * t * t * t * -3.65988281559036e-31);
        }

        // The driver takes words 4 to 11 as the eight coefficients, then the
        // high bytes of word 12 and 13 as bits 16 to 23 of C4, C3, C7 and C5.
        // Word 15 is what makes the CRC-8 over the whole ROM come out zero.
        private static ushort[] BuildRom()
        {
            var words = new ushort[RomWords];
            words[4] = (ushort)C0;
            words[5] = (ushort)C1;
            words[6] = (ushort)C2;
            words[7] = (ushort)(C3 & 0xFFFF);
            words[8] = (ushort)(C4 & 0xFFFF);
            words[9] = (ushort)C5;
            words[10] = (ushort)C6;
            words[11] = (ushort)C7;
            words[12] = (ushort)(((C4 >> 16) << 8) | (C3 >> 16));
            words[13] = (ushort)(((C7 >> 16) << 8) | (C5 >> 16));
            words[15] = CrcCarrier;
            return words;
        }

        // Coefficients fitted so that the driver's own compensation reads back
        // 300 to 1200 mBar over -20 to 60 deg C from counts inside the part's
        // 24 bits, with the cross terms left at zero.
        private const int C0 = 3310;
        private const int C1 = 0;
        private const int C2 = 10987;
        private const int C3 = 459408;
        private const int C4 = 1742934;
        private const int C5 = 0;
        private const int C6 = 0;
        private const int C7 = 0;
        private const ushort CrcCarrier = 0x008C;

        private const int RomWords = 16;
        private const byte ResetCommand = 0x10;
        private const byte RomReadBase = 0xE0;
        private const byte ConfigWrite0 = 0x20;
        private const byte ConfigWrite1 = 0x22;
        private const byte ConvertCommand = 0x40;
        private const byte AdcReadCommand = 0x50;
        private const byte ChannelMask = 0x0F;
        private const byte PressureChannel = 0x04;
        private const byte TemperatureChannel = 0x08;

        private const double CountsLow = 2.0e5;
        private const double CountsHigh = 1.6e6;
        private const double CountsMidpoint = 1.0e6;
        private const int BisectionSteps = 60;
        private const int InversionRounds = 10;

        private ushort[] configuration;
        private uint pressureCounts;
        private uint temperatureCounts;
        private byte[] stream;
        private int taken;
        private readonly ushort[] rom;
    }
}
