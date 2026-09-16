//
// nRF SPIM (EasyDMA) controller for spi2. Renode's stock NRF52840_SPI doesn't
// run the EasyDMA transfer (so the flash driver spun on EVENTS_END and the
// reset/JEDEC detect never completed). This model performs the DMA on
// TASKS_START, raises EVENTS_END, and records the byte stream.
//
// Two devices share this bus, told apart by chip select: the MX25R SPI-NOR
// flash on P0.15 and the ADXL367 accelerometer on P0.16. The flash stays inside
// the controller because the scripts drive it through this object
// (LoadImage/DumpStream/ReadImageWord) and because its session state is what
// makes the SPI-NOR command stream readable at all; every other chip select
// routes to an ISPIPeripheral registered at that pin number, so the ADXL367 is
// an ordinary registered device.
//
using System;
using System.IO;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Core.Structure;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.SPI
{
    public class NrfSpimCapture : SimpleContainer<ISPIPeripheral>, IDoubleWordPeripheral, IKnownSize, INumberedGPIOOutput, IGPIOReceiver
    {
        public NrfSpimCapture(IMachine machine) : base(machine)
        {
            this.sysbus = machine.GetSystemBus(this);
            regs = new Dictionary<long, uint>();
            stream = new List<byte>();
            cmdHist = new Dictionary<byte, long>();
            unknownCommands = new HashSet<byte>();
            IRQ = new GPIO();
            Connections = new Dictionary<int, IGPIO> { { 0, IRQ } };
        }

        public GPIO IRQ { get; private set; }
        public IReadOnlyDictionary<int, IGPIO> Connections { get; private set; }

        public byte PollReply { get; set; }  // default reply for unhandled reads

        // Chip select, active low, one pin per device: P0.15 is the flash, every
        // other pin is looked up among the registered devices. The driver splits
        // one command across several EasyDMA transfers while CS stays asserted,
        // so a command only makes sense per CS session, not per transfer.
        public void OnGPIO(int number, bool value)
        {
            ISPIPeripheral device;
            var known = number == FlashChipSelectPin || TryGetByAddress(number, out device);
            if(!known)
            {
                return;
            }
            if(!value)
            {
                if(selectedPin != NoChipSelect && selectedPin != number)
                {
                    this.Log(LogLevel.Error, "chip select P0.{0} asserted while P0.{1} still is: both devices drive MISO", number, selectedPin);
                }
                selectedPin = number;
                StartSession();
                return;
            }
            if(selectedPin != number)
            {
                return;
            }
            if(TryGetByAddress(selectedPin, out device))
            {
                device.FinishTransmission();
            }
            selectedPin = NoChipSelect;
        }

        private void StartSession()
        {
            sessionIndex = 0;
            sessionCommand = NoCommand;
            sessionAddress = 0;
            sessionWriteAllowed = false;
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

        public override void Reset()
        {
            regs.Clear();
            stream.Clear();
            endFlag = false;
            inten = 0;
            selectedPin = NoChipSelect;
            sessionIndex = 0;
            sessionCommand = NoCommand;
            sessionAddress = 0;
            writeEnableLatch = false;
            warnedNoChipSelect = false;
            unknownCommands.Clear();
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
            if(selectedPin == NoChipSelect)
            {
                if(!warnedNoChipSelect)
                {
                    warnedNoChipSelect = true;
                    this.Log(LogLevel.Error, "transfer with no chip select asserted: no device is listening, the transfer reads as zero");
                }
                regs[TxAmount] = Get(TxCnt);
                regs[RxAmount] = Get(RxCnt);
                endFlag = true;
                UpdateIrq();
                return;
            }
            ISPIPeripheral device;
            var toDevice = TryGetByAddress(selectedPin, out device);
            uint txp = Get(TxPtr), txn = Get(TxCnt), rxp = Get(RxPtr), rxn = Get(RxCnt);
            uint n = Math.Max(txn, rxn);
            var tx = new byte[txn];
            for(uint i = 0; i < txn; i++)
            {
                tx[i] = sysbus.ReadByte((ulong)(txp + i));
                if(!toDevice)
                {
                    stream.Add(tx[i]);
                }
            }
            var rx = new byte[rxn];
            for(uint i = 0; i < n; i++)
            {
                byte outgoing = i < txn ? tx[i] : (byte)0x00;
                byte incoming = toDevice ? device.Transmit(outgoing) : ServeByte(outgoing);
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
                if(outgoing == (byte)FlashCommand.WriteEnable)
                {
                    writeEnableLatch = true;
                }
                else if(outgoing == (byte)FlashCommand.WriteDisable)
                {
                    writeEnableLatch = false;
                }
                else if(outgoing == (byte)FlashCommand.ChipErase60 || outgoing == (byte)FlashCommand.ChipEraseC7)
                {
                    if(ConsumeWriteEnable("chip erase", 0))
                    {
                        this.Log(LogLevel.Warning, "chip erase: the whole image the boot reads from is now 0xFF");
                        Erase(0, flash == null ? 0 : flash.Length);
                    }
                }
                return 0x00;
            }
            switch((FlashCommand)sessionCommand)
            {
            case FlashCommand.ReadId:
                return index <= 3 ? JedecId[index - 1] : (byte)0x00;
            case FlashCommand.Read:
                return ReadByteAt(index, 1, 4, outgoing);
            case FlashCommand.FastRead:
                return ReadByteAt(index, 1, 5, outgoing);
            case FlashCommand.ReadStatus:
                return writeEnableLatch ? WriteEnableLatchBit : (byte)0x00;
            case FlashCommand.PageProgram:
                ProgramByte(index, outgoing);
                return 0x00;
            case FlashCommand.SectorErase:
            case FlashCommand.BlockErase32K:
            case FlashCommand.BlockErase64K:
                EraseAt(index, outgoing);
                return 0x00;
            case FlashCommand.ResetEnable:
            case FlashCommand.Reset:
            case FlashCommand.WriteEnable:
            case FlashCommand.WriteDisable:
            case FlashCommand.WriteStatus:
            case FlashCommand.ChipErase60:
            case FlashCommand.ChipEraseC7:
                return 0x00;
            default:
                if(unknownCommands.Add((byte)sessionCommand))
                {
                    this.Log(LogLevel.Debug, "unhandled SPI-NOR command 0x{0:X2}, answering PollReply", sessionCommand);
                }
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

        // Program data starts at byte 4 (command + three address bytes) and wraps
        // inside the 256-byte page, as the MX25R datasheet specifies.
        private void ProgramByte(int index, byte outgoing)
        {
            if(index < 4)
            {
                sessionAddress = (sessionAddress << 8) | outgoing;
                if(index == 3)
                {
                    sessionWriteAllowed = ConsumeWriteEnable("page program", sessionAddress);
                }
                return;
            }
            if(!sessionWriteAllowed || flash == null)
            {
                return;
            }
            int offsetInPage = (sessionAddress + index - 4) % PageSize;
            int a = (sessionAddress & ~(PageSize - 1)) + offsetInPage;
            if(a < 0 || a >= flash.Length)
            {
                return;
            }
            flash[a] &= outgoing;
        }

        private void EraseAt(int index, byte outgoing)
        {
            if(index >= 4)
            {
                return;
            }
            sessionAddress = (sessionAddress << 8) | outgoing;
            if(index != 3)
            {
                return;
            }
            int size = sessionCommand == (int)FlashCommand.SectorErase ? SectorSize
                : sessionCommand == (int)FlashCommand.BlockErase32K ? Block32KSize : Block64KSize;
            if(!ConsumeWriteEnable("erase", sessionAddress))
            {
                return;
            }
            Erase(sessionAddress & ~(size - 1), size);
        }

        // The model completes program/erase within the transfer, so WIP is never
        // observable and WEL falls at the same moment the operation finishes.
        private bool ConsumeWriteEnable(string operation, int address)
        {
            if(!writeEnableLatch)
            {
                this.Log(LogLevel.Error, "{0} at 0x{1:X} ignored: write enable latch is clear", operation, address);
                return false;
            }
            writeEnableLatch = false;
            return true;
        }

        private void Erase(int start, int size)
        {
            if(flash == null)
            {
                return;
            }
            for(int a = start; a < start + size && a < flash.Length; a++)
            {
                flash[a] = 0xFF;
            }
        }

        public uint ReadImageWord(uint address)
        {
            if(flash == null || address + 4 > flash.Length)
            {
                throw new RecoverableException("no flash image loaded, or address out of range");
            }
            return (uint)(flash[address] | (flash[address + 1] << 8) | (flash[address + 2] << 16) | (flash[address + 3] << 24));
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

        private const int PageSize = 256;
        private const int SectorSize = 4096;
        private const int Block32KSize = 32 * 1024;
        private const int Block64KSize = 64 * 1024;
        private const byte WriteEnableLatchBit = 1 << 1;

        private enum FlashCommand : byte
        {
            WriteStatus = 0x01,
            PageProgram = 0x02,
            Read = 0x03,
            WriteDisable = 0x04,
            ReadStatus = 0x05,
            WriteEnable = 0x06,
            FastRead = 0x0B,
            SectorErase = 0x20,
            BlockErase32K = 0x52,
            ChipErase60 = 0x60,
            ResetEnable = 0x66,
            BlockErase64K = 0xD8,
            Reset = 0x99,
            ReadId = 0x9F,
            ChipEraseC7 = 0xC7,
        }

        private const int FlashChipSelectPin = 15;
        private const int NoChipSelect = -1;
        private const int NoCommand = -1;
        private static readonly byte[] JedecId = { 0xC2, 0x28, 0x17 };

        private byte[] flash;
        private int selectedPin = NoChipSelect;
        private bool writeEnableLatch;
        private bool sessionWriteAllowed;
        private bool warnedNoChipSelect;
        private int sessionIndex;
        private int sessionCommand;
        private int sessionAddress;
        private uint inten;
        private bool endFlag;
        private readonly Dictionary<long, uint> regs;
        private readonly Dictionary<byte, long> cmdHist;
        private readonly List<byte> stream;
        private readonly HashSet<byte> unknownCommands;
        private readonly IBusController sysbus;
    }
}
