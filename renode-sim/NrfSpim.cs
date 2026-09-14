//
// Minimal nRF SPIM (EasyDMA) controller for the HWA10 sim. Renode's stock
// NRF52840_SPI doesn't run the EasyDMA transfer for this firmware, so the OLED
// driver spins on EVENTS_END forever. This model performs the DMA on TASKS_START:
// it reads the Tx buffer from memory, answers the poll on the Rx buffer, raises
// EVENTS_END, and records the byte stream + D/C for framebuffer reconstruction.
//
using System;
using System.IO;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.SPI
{
    public class NrfSpimCapture : IDoubleWordPeripheral, IKnownSize
    {
        public NrfSpimCapture(IMachine machine)
        {
            this.machine = machine;
            this.sysbus = machine.GetSystemBus(this);
            regs = new Dictionary<long, uint>();
            stream = new List<byte>();
            dc = new List<bool>();
        }

        public byte PollReply { get; set; }
        public long DcPort { get; set; }   // GPIO OUT register (0x50000504 P0 / 0x50000804 P1), 0 = ignore
        public int DcBit { get; set; }

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
            }
        }

        public void Reset()
        {
            regs.Clear();
            stream.Clear();
            dc.Clear();
            endFlag = false;
        }

        public void DumpStream(string path)
        {
            using(var f = new StreamWriter(path))
            {
                for(int i = 0; i < stream.Count; i++)
                {
                    f.WriteLine((dc[i] ? "D " : "C ") + stream[i].ToString("X2"));
                }
            }
            this.Log(LogLevel.Info, "OLED stream: {0} bytes -> {1}", stream.Count, path);
        }

        private void DoTransfer()
        {
            uint txp = Get(TxPtr), txn = Get(TxCnt), rxp = Get(RxPtr), rxn = Get(RxCnt);
            bool d = false;
            if(DcPort != 0)
            {
                d = ((sysbus.ReadDoubleWord((ulong)DcPort) >> DcBit) & 1u) != 0;
            }
            uint n = Math.Max(txn, rxn);
            for(uint i = 0; i < n; i++)
            {
                if(i < txn)
                {
                    stream.Add(sysbus.ReadByte((ulong)(txp + i)));
                    dc.Add(d);
                }
                if(i < rxn)
                {
                    sysbus.WriteByte((ulong)(rxp + i), PollReply);
                }
            }
            endFlag = true;
            if(stream.Count % 4096 == 0)
            {
                this.Log(LogLevel.Info, "OLED stream at {0} bytes", stream.Count);
            }
        }

        private uint Get(long off)
        {
            uint v;
            return regs.TryGetValue(off, out v) ? v : 0u;
        }

        public long Size { get { return 0x1000; } }

        private const long TasksStart = 0x10;
        private const long EventsEnd = 0x118;
        private const long RxPtr = 0x534;
        private const long RxCnt = 0x538;
        private const long TxPtr = 0x544;
        private const long TxCnt = 0x548;

        private bool endFlag;
        private readonly Dictionary<long, uint> regs;
        private readonly List<byte> stream;
        private readonly List<bool> dc;
        private readonly IMachine machine;
        private readonly IBusController sysbus;
    }
}
