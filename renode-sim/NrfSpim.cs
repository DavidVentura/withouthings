//
// nRF SPIM (EasyDMA) controller for spi2 = the external MX25R SPI-NOR flash.
// Renode's stock NRF52840_SPI doesn't run the EasyDMA transfer (so the flash
// driver spun on EVENTS_END and the reset/JEDEC detect never completed). This
// model performs the DMA on TASKS_START, answers the SPI-NOR command protocol
// from a real 8 MB flash dump, raises EVENTS_END, and records the byte stream.
//
using System;
using System.IO;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Core.Structure;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.SPI
{
    public class NrfSpimCapture : IDoubleWordPeripheral, IKnownSize, INumberedGPIOOutput
    {
        public NrfSpimCapture(IMachine machine)
        {
            this.machine = machine;
            this.sysbus = machine.GetSystemBus(this);
            regs = new Dictionary<long, uint>();
            stream = new List<byte>();
            cmdHist = new Dictionary<byte, long>();
            IRQ = new GPIO();
            Connections = new Dictionary<int, IGPIO> { { 0, IRQ } };
        }

        public GPIO IRQ { get; private set; }
        public IReadOnlyDictionary<int, IGPIO> Connections { get; private set; }

        public byte PollReply { get; set; }  // default reply for unhandled reads

        // Called from the script: `sysbus.spi2 LoadImage @external_flash.bin`.
        public void LoadImage(string path)
        {
            flash = File.ReadAllBytes(path);
            this.Log(LogLevel.Info, "flash image loaded: {0} bytes from {1}", flash.Length, path);
        }

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

        private void UpdateIrq()
        {
            IRQ.Set(endFlag && (inten & EndIntBit) != 0);
        }

        public void Reset()
        {
            regs.Clear();
            stream.Clear();
            endFlag = false;
        }

        public void DumpCmds(string path)
        {
            using(var f = new StreamWriter(path))
            {
                foreach(var kv in cmdHist)
                {
                    f.WriteLine("{0:X2} {1}", kv.Key, kv.Value);
                }
            }
        }

        public void DumpStream(string path)
        {
            using(var f = new StreamWriter(path))
            {
                foreach(var b in stream)
                {
                    f.WriteLine(b.ToString("X2"));
                }
            }
            this.Log(LogLevel.Info, "spi2 stream: {0} bytes -> {1}", stream.Count, path);
        }

        private void DoTransfer()
        {
            uint txp = Get(TxPtr), txn = Get(TxCnt), rxp = Get(RxPtr), rxn = Get(RxCnt);
            var tx = new byte[txn];
            for(uint i = 0; i < txn; i++)
            {
                tx[i] = sysbus.ReadByte((ulong)(txp + i));
                stream.Add(tx[i]);
            }
            var rx = new byte[rxn];
            ServeFlash(tx, rx);
            for(uint i = 0; i < rxn; i++)
            {
                sysbus.WriteByte((ulong)(rxp + i), rx[i]);
            }
            endFlag = true;
            UpdateIrq();
        }

        // SPI-NOR command protocol. Full-duplex: rx[i] answers tx[i] (data trails
        // the command+address+dummy bytes).
        private void ServeFlash(byte[] tx, byte[] rx)
        {
            byte cmd = tx.Length > 0 ? tx[0] : (byte)0;
            long c;
            cmdHist[cmd] = cmdHist.TryGetValue(cmd, out c) ? c + 1 : 1;
            switch(cmd)
            {
            case 0x9F:  // RDID -> MX25R6435F
                Put(rx, 1, 0xC2); Put(rx, 2, 0x28); Put(rx, 3, 0x17);
                break;
            case 0x03:  // READ (24-bit addr), data from rx[4]
                ReadFlash(Addr24(tx, 1), rx, 4);
                break;
            case 0x0B:  // FAST READ (24-bit addr + 1 dummy), data from rx[5]
                ReadFlash(Addr24(tx, 1), rx, 5);
                break;
            case 0x05:  // RDSR -> status 0x00 (not busy, not WEL)
                for(int i = 1; i < rx.Length; i++) rx[i] = 0x00;
                break;
            case 0x66:  // Reset-Enable
            case 0x99:  // Reset
            case 0x06:  // WREN
            case 0x04:  // WRDI
                break;  // ack only
            default:    // RDCR(0x35), security, etc. -> benign default
                for(int i = 1; i < rx.Length; i++) rx[i] = PollReply;
                break;
            }
        }

        private void ReadFlash(int addr, byte[] rx, int dataStart)
        {
            if(flash == null) return;
            for(int i = dataStart; i < rx.Length; i++)
            {
                int a = addr + (i - dataStart);
                rx[i] = (a >= 0 && a < flash.Length) ? flash[a] : (byte)0xFF;
            }
        }

        private static int Addr24(byte[] tx, int at)
        {
            int a = 0;
            for(int i = 0; i < 3; i++)
            {
                a = (a << 8) | (at + i < tx.Length ? tx[at + i] : 0);
            }
            return a;
        }

        private static void Put(byte[] rx, int i, byte v)
        {
            if(i < rx.Length) rx[i] = v;
        }

        private uint Get(long off)
        {
            uint v;
            return regs.TryGetValue(off, out v) ? v : 0u;
        }

        public long Size { get { return 0x1000; } }

        private const long TasksStart = 0x10;
        private const long EventsEnd = 0x118;
        private const long IntenSet = 0x304;
        private const long IntenClr = 0x308;
        private const uint EndIntBit = 1u << 6;   // SPIM INTEN END bit
        private const long RxPtr = 0x534;
        private const long RxCnt = 0x538;
        private const long TxPtr = 0x544;
        private const long TxCnt = 0x548;

        private byte[] flash;
        private uint inten;
        private bool endFlag;
        private readonly Dictionary<long, uint> regs;
        private readonly Dictionary<byte, long> cmdHist;
        private readonly List<byte> stream;
        private readonly IMachine machine;
        private readonly IBusController sysbus;
    }
}
