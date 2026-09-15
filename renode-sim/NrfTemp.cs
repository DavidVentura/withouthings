//
// nRF52840 TEMP. Renode 1.17 ships no model, so the SVD stub left EVENTS_DATARDY
// at 0 and the nrfx temperature read never returned. A die measurement settles in
// well under a scheduler tick, so TASKS_START latches the result immediately.
//
using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Core.Structure;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Sensors
{
    public class NrfTemp : IDoubleWordPeripheral, IKnownSize, INumberedGPIOOutput
    {
        public NrfTemp(IMachine machine)
        {
            this.machine = machine;
            regs = new Dictionary<long, uint>();
            IRQ = new GPIO();
            Connections = new Dictionary<int, IGPIO> { { 0, IRQ } };
            TemperatureCelsius = 25.0;
            Reset();
        }

        public GPIO IRQ { get; private set; }
        public IReadOnlyDictionary<int, IGPIO> Connections { get; private set; }

        // Kept out of Reset() so a value set from the run script survives the
        // SYSRESETREQ the app's fault handler issues.
        public double TemperatureCelsius { get; set; }

        public uint ReadDoubleWord(long offset)
        {
            if(offset == (long)Reg.Temp)
            {
                return temperature;
            }
            if(offset == (long)Reg.EventsDataRdy || IsCalibration(offset))
            {
                return Get(offset);
            }
            if(offset == (long)Reg.IntenSet || offset == (long)Reg.IntenClr)
            {
                return inten;
            }
            this.Log(LogLevel.Warning, "Read from unhandled TEMP offset 0x{0:X}", offset);
            return 0;
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            if(IsCalibration(offset))
            {
                regs[offset] = value;
                return;
            }
            switch((Reg)offset)
            {
            case Reg.TasksStart:
                temperature = (uint)(int)Math.Round(TemperatureCelsius * 4.0);
                regs[(long)Reg.EventsDataRdy] = 1;
                UpdateIrq();
                break;
            case Reg.TasksStop:
                break;
            case Reg.EventsDataRdy:
                regs[offset] = value;
                UpdateIrq();
                break;
            case Reg.IntenSet:
                inten |= value;
                UpdateIrq();
                break;
            case Reg.IntenClr:
                inten &= ~value;
                UpdateIrq();
                break;
            default:
                this.Log(LogLevel.Warning, "Write 0x{0:X} to unhandled TEMP offset 0x{1:X}", value, offset);
                break;
            }
        }

        private void UpdateIrq()
        {
            IRQ.Set(Get((long)Reg.EventsDataRdy) != 0 && (inten & DataRdyIntenBit) != 0);
        }

        private bool IsCalibration(long offset)
        {
            if(offset >= (long)Reg.A0 && offset <= (long)Reg.A5)
            {
                return true;
            }
            if(offset >= (long)Reg.B0 && offset <= (long)Reg.B5)
            {
                return true;
            }
            return offset >= (long)Reg.T0 && offset <= (long)Reg.T4;
        }

        public void Reset()
        {
            regs.Clear();
            inten = 0;
            temperature = 0;
            IRQ.Set(false);
        }

        private uint Get(long offset)
        {
            uint v;
            return regs.TryGetValue(offset, out v) ? v : 0u;
        }

        public long Size { get { return 0x1000; } }

        private const uint DataRdyIntenBit = 1u << 0;

        private enum Reg : long
        {
            TasksStart = 0x000,
            TasksStop = 0x004,
            EventsDataRdy = 0x100,
            IntenSet = 0x304,
            IntenClr = 0x308,
            Temp = 0x508,
            A0 = 0x520,
            A5 = 0x534,
            B0 = 0x540,
            B5 = 0x554,
            T0 = 0x560,
            T4 = 0x570,
        }

        private uint inten;
        private uint temperature;
        private readonly Dictionary<long, uint> regs;
        private readonly IMachine machine;
    }
}
