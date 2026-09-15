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
    public class NrfSpimCapture : IDoubleWordPeripheral, IKnownSize, INumberedGPIOOutput, IGPIOReceiver
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

        // Chip select (P0.15), active low. The driver splits one SPI-NOR command
        // across several EasyDMA transfers while CS stays asserted, so the command
        // only makes sense per CS session, not per transfer.
        public void OnGPIO(int number, bool value)
        {
            if(number != ChipSelectPin)
            {
                return;
            }
            if(value)
            {
                selected = false;
                return;
            }
            StartSession();
        }

        private void StartSession()
        {
            selected = true;
            sessionIndex = 0;
            sessionCommand = NoCommand;
            sessionAddress = 0;
        }

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
            inten = 0;
            selected = false;
            sessionIndex = 0;
            sessionCommand = NoCommand;
            sessionAddress = 0;
            warnedNoChipSelect = false;
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
            if(!selected)
            {
                if(!warnedNoChipSelect)
                {
                    warnedNoChipSelect = true;
                    this.Log(LogLevel.Error, "transfer with chip select deasserted: P0.15 is not wired to this peripheral, SPI-NOR commands cannot be tracked");
                }
                StartSession();
            }
            uint n = Math.Max(txn, rxn);
            var tx = new byte[txn];
            for(uint i = 0; i < txn; i++)
            {
                tx[i] = sysbus.ReadByte((ulong)(txp + i));
                stream.Add(tx[i]);
            }
            var rx = new byte[rxn];
            for(uint i = 0; i < n; i++)
            {
                byte outgoing = i < txn ? tx[i] : (byte)0x00;
                byte incoming = ServeByte(outgoing);
                if(i < rxn)
                {
                    rx[i] = incoming;
                }
            }
            for(uint i = 0; i < rxn; i++)
            {
                sysbus.WriteByte((ulong)(rxp + i), rx[i]);
            }
            // report bytes actually transferred; the driver polls TXD/RXD.AMOUNT for progress
            regs[TxAmount] = txn;
            regs[RxAmount] = rxn;
            endFlag = true;
            UpdateIrq();
        }

        // SPI-NOR command protocol, one byte at a time within the current CS session.
        // Full duplex: the returned byte is clocked in while `outgoing` is clocked out.
        private byte ServeByte(byte outgoing)
        {
            int index = sessionIndex;
            sessionIndex++;
            if(index == 0)
            {
                sessionCommand = outgoing;
                long c;
                cmdHist[outgoing] = cmdHist.TryGetValue(outgoing, out c) ? c + 1 : 1;
                return 0x00;
            }
            switch(sessionCommand)
            {
            case 0x9F:  // RDID -> MX25R6435F
                return index <= 3 ? JedecId[index - 1] : (byte)0x00;
            case 0x03:  // READ (24-bit addr), data from byte 4
                return ReadByteAt(index, 1, 4, outgoing);
            case 0x0B:  // FAST READ (24-bit addr + 1 dummy), data from byte 5
                return ReadByteAt(index, 1, 5, outgoing);
            case 0x05:  // RDSR -> status 0x00 (not busy, not WEL)
                return 0x00;
            case 0x66:  // Reset-Enable
            case 0x99:  // Reset
            case 0x06:  // WREN
            case 0x04:  // WRDI
            case 0x01:  // WRSR + data
                return 0x00;
            default:    // RDCR(0x15/0x35), security, etc. -> benign default
                return PollReply;
            }
        }

        private byte ReadByteAt(int index, int addrStart, int dataStart, byte outgoing)
        {
            if(index >= addrStart && index < addrStart + 3)
            {
                sessionAddress = (sessionAddress << 8) | outgoing;
                return 0x00;
            }
            if(index < dataStart)
            {
                return 0x00;
            }
            if(flash == null)
            {
                return 0xFF;
            }
            int a = sessionAddress + (index - dataStart);
            return a >= 0 && a < flash.Length ? flash[a] : (byte)0xFF;
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
        private const long RxAmount = 0x53C;
        private const long TxPtr = 0x544;
        private const long TxCnt = 0x548;
        private const long TxAmount = 0x54C;

        private const int ChipSelectPin = 15;
        private const int NoCommand = -1;
        private static readonly byte[] JedecId = { 0xC2, 0x28, 0x17 };

        private byte[] flash;
        private bool selected;
        private bool warnedNoChipSelect;
        private int sessionIndex;
        private int sessionCommand;
        private int sessionAddress;
        private uint inten;
        private bool endFlag;
        private readonly Dictionary<long, uint> regs;
        private readonly Dictionary<byte, long> cmdHist;
        private readonly List<byte> stream;
        private readonly IMachine machine;
        private readonly IBusController sysbus;
    }
}
