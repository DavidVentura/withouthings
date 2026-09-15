//
// TI BQ25180 single-cell charger on TWI0 at 0x6A. Without it the firmware's
// STAT0 read fails, the battery module reports its I2C-error state, and the UI
// sits on the charger page. The register file is the datasheet's; STAT0 is
// synthesised from the VIN/charging inputs and the CHG_DIS bit the driver
// writes, since that is the only register whose value the firmware acts on.
//
using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.I2C;

namespace Antmicro.Renode.Peripherals.Sensors
{
    public class BQ25180 : II2CPeripheral
    {
        public BQ25180()
        {
            registers = new byte[RegisterCount];
            Reset();
        }

        // Inputs, set from the run script. Kept out of Reset() so they survive the
        // SYSRESETREQ the app's fault handler issues. The default is a watch on the
        // wrist: no charger connected.
        public bool VinPresent { get; set; }
        public bool Charging { get; set; }

        public void Write(byte[] data)
        {
            foreach(var value in data)
            {
                if(expectRegisterPointer)
                {
                    if(value >= RegisterCount)
                    {
                        this.Log(LogLevel.Warning, "Access to undefined register 0x{0:X2}", value);
                    }
                    pointer = value;
                    expectRegisterPointer = false;
                    continue;
                }
                WriteRegister(value);
            }
        }

        public byte[] Read(int count = 1)
        {
            var result = new byte[count];
            for(var i = 0; i < count; i++)
            {
                result[i] = ReadRegister();
            }
            return result;
        }

        // A repeated start for the data phase of a register read ends the address
        // phase here, so the pointer survives but the next write is an address again.
        public void FinishTransmission()
        {
            expectRegisterPointer = true;
        }

        public void Reset()
        {
            registers[(int)Register.Stat0] = 0x00;
            registers[(int)Register.Stat1] = 0x00;
            registers[(int)Register.Flag0] = 0x00;
            registers[(int)Register.VbatCtrl] = 0x46;
            registers[(int)Register.IchgCtrl] = 0x05;
            registers[(int)Register.ChargeCtrl0] = 0x2C;
            registers[(int)Register.ChargeCtrl1] = 0x40;
            registers[(int)Register.IcCtrl] = 0x84;
            registers[(int)Register.TmrIlim] = 0x4D;
            registers[(int)Register.ShipRst] = 0x11;
            registers[(int)Register.SysReg] = 0x40;
            registers[(int)Register.TsControl] = 0x00;
            registers[(int)Register.MaskId] = (byte)(0xC0 | DeviceId);
            pointer = 0;
            expectRegisterPointer = true;
        }

        private byte ReadRegister()
        {
            if(pointer >= RegisterCount)
            {
                this.Log(LogLevel.Warning, "Read from undefined register 0x{0:X2}", pointer);
                return 0;
            }
            var value = (Register)pointer == Register.Stat0 ? Stat0() : registers[pointer];
            this.Log(LogLevel.Noisy, "Read 0x{0:X2} from {1}", value, (Register)pointer);
            Advance();
            return value;
        }

        private void WriteRegister(byte value)
        {
            if(pointer >= RegisterCount)
            {
                this.Log(LogLevel.Warning, "Write 0x{0:X2} to undefined register 0x{1:X2}", value, pointer);
                return;
            }
            var register = (Register)pointer;
            if(register == Register.Stat0 || register == Register.Stat1)
            {
                this.Log(LogLevel.Warning, "Write 0x{0:X2} to read-only {1}", value, register);
                Advance();
                return;
            }
            this.Log(LogLevel.Noisy, "Write 0x{0:X2} to {1}", value, register);
            registers[pointer] = register == Register.MaskId ? (byte)((value & 0xF0) | DeviceId) : value;
            Advance();
        }

        private void Advance()
        {
            pointer = (byte)((pointer + 1) % RegisterCount);
        }

        private byte Stat0()
        {
            if(!VinPresent)
            {
                return 0;
            }
            var chargeDisabled = (registers[(int)Register.IchgCtrl] & ChgDisBit) != 0;
            var status = Charging && !chargeDisabled ? ChargeStatus.ConstantCurrent : ChargeStatus.DoneOrDisabled;
            return (byte)(VinPGoodBit | ((byte)status << ChargeStatusShift));
        }

        private enum ChargeStatus : byte
        {
            NotCharging = 0,
            ConstantCurrent = 1,
            ConstantVoltage = 2,
            DoneOrDisabled = 3,
        }

        private enum Register : byte
        {
            Stat0 = 0x00,
            Stat1 = 0x01,
            Flag0 = 0x02,
            VbatCtrl = 0x03,
            IchgCtrl = 0x04,
            ChargeCtrl0 = 0x05,
            ChargeCtrl1 = 0x06,
            IcCtrl = 0x07,
            TmrIlim = 0x08,
            ShipRst = 0x09,
            SysReg = 0x0A,
            TsControl = 0x0B,
            MaskId = 0x0C,
        }

        private const int RegisterCount = 0x0D;
        private const byte DeviceId = 0x0;
        private const byte ChgDisBit = 1 << 7;
        private const byte VinPGoodBit = 1 << 0;
        private const int ChargeStatusShift = 5;

        private byte pointer;
        private bool expectRegisterPointer;
        private readonly byte[] registers;
    }
}
