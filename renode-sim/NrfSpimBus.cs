//
// nRF SPIM (EasyDMA) controller for instance 1 at 0x40004000, the bus the
// MAX8617x optical front end hangs on. It was modelled as TWI here, but the
// firmware drives it as a SPIM: it arms TXD/RXD and TASKS_START on it, so the
// completion event never came and the task that probes the front end blocked
// on its transfer semaphore for the rest of the run.
//
// The other two SPIM models in this rig have their device baked in because each
// answers a protocol of its own; this one takes a registered SPI device, and no
// device is attached yet, so the front end reads as absent (undriven MISO, part
// id 0x00) and the firmware logs it and carries on. A model of the part belongs
// on this bus, with its FIFO and its interrupt line: the driver arms a 10 s
// software watchdog once it accepts a part id, and a part that answers the
// probe but never delivers samples resets the watch.
//
// Renode's stock SPI models do not run the EasyDMA transfer, so a driver that
// arms TXD/RXD and writes TASKS_START never sees EVENTS_END. Here the transfer
// runs on TASKS_START and the END event (and its interrupt) is raised from it.
//
using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Core.Structure;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.SPI
{
    public class NrfSpimBus : SimpleContainer<ISPIPeripheral>, IDoubleWordPeripheral, IKnownSize, INumberedGPIOOutput
    {
        public NrfSpimBus(IMachine machine) : base(machine)
        {
            this.sysbus = machine.GetSystemBus(this);
            regs = new Dictionary<long, uint>();
            IRQ = new GPIO();
            Connections = new Dictionary<int, IGPIO> { { 0, IRQ } };
        }

        public GPIO IRQ { get; private set; }
        public IReadOnlyDictionary<int, IGPIO> Connections { get; private set; }

        public uint ReadDoubleWord(long offset)
        {
            if(offset == EventsEnd)
            {
                return endFlag ? 1u : 0u;
            }
            uint v;
            return regs.TryGetValue(offset, out v) ? v : 0u;
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            regs[offset] = value;
            if(offset == TasksStart)
            {
                DoTransfer();
            }
            else if(offset == EventsEnd && value == 0)
            {
                endFlag = false;
                UpdateIrq();
            }
            else if(offset == IntenSet)
            {
                inten |= value;
            }
            else if(offset == IntenClr)
            {
                inten &= ~value;
                UpdateIrq();
            }
        }

        public override void Reset()
        {
            regs.Clear();
            endFlag = false;
            inten = 0;
            warnedNoDevice = false;
        }

        private void DoTransfer()
        {
            ISPIPeripheral device;
            var attached = TryGetByAddress(0, out device);
            if(!attached && !warnedNoDevice)
            {
                warnedNoDevice = true;
                this.Log(LogLevel.Warning, "no device registered on this bus: MISO is undriven, so every transfer reads as zero");
            }
            uint txp = Get(TxPtr), txn = Get(TxCnt), rxp = Get(RxPtr), rxn = Get(RxCnt);
            uint n = Math.Max(txn, rxn);
            for(uint i = 0; i < n; i++)
            {
                var outgoing = i < txn ? sysbus.ReadByte((ulong)(txp + i)) : (byte)0x00;
                var incoming = attached ? device.Transmit(outgoing) : (byte)0x00;
                if(i < rxn)
                {
                    sysbus.WriteByte((ulong)(rxp + i), incoming);
                }
            }
            // Chip select is deasserted between EasyDMA transfers on this bus
            // (the driver runs one frame per transfer), so the device's frame
            // ends here.
            if(attached)
            {
                device.FinishTransmission();
            }
            regs[TxAmount] = txn;
            regs[RxAmount] = rxn;
            endFlag = true;
            UpdateIrq();
        }

        private void UpdateIrq()
        {
            IRQ.Set(endFlag && (inten & EndIntBit) != 0);
        }

        private uint Get(long offset)
        {
            uint v;
            return regs.TryGetValue(offset, out v) ? v : 0u;
        }

        public long Size => 0x1000;

        private const long TasksStart = 0x010;
        private const long EventsEnd = 0x118;
        private const long IntenSet = 0x304;
        private const long IntenClr = 0x308;
        private const long RxPtr = 0x534;
        private const long RxCnt = 0x538;
        private const long RxAmount = 0x53C;
        private const long TxPtr = 0x544;
        private const long TxCnt = 0x548;
        private const long TxAmount = 0x54C;
        private const uint EndIntBit = 1u << 6;

        private bool endFlag;
        private bool warnedNoDevice;
        private uint inten;
        private readonly Dictionary<long, uint> regs;
        private readonly IBusController sysbus;
    }
}
