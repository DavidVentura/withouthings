//
// nRF SPIM (EasyDMA) capture for the display bus. Renode's stock NRF52840_SPI
// does not run the EasyDMA transfer, so a display driver that pushes a frame
// over EasyDMA never advances (no EVENTS_END, no bytes moved). This model runs
// the DMA on TASKS_START, raises EVENTS_END, and records every outgoing byte
// together with the state of the data/command (D/C) GPIO at the time of the
// transfer, so the command stream and the pixel payload can be separated and a
// framebuffer reconstructed.
//
// It is a capture, not a panel emulation: it does not answer reads with any
// controller state. Reconstruction (controller opcodes, resolution, bit depth)
// is done offline from the dumped stream.
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
    public enum DataCommand
    {
        Undriven,
        Command,
        Data
    }

    public class OledSpimCapture : IDoubleWordPeripheral, IKnownSize, INumberedGPIOOutput, IGPIOReceiver
    {
        public OledSpimCapture(IMachine machine)
        {
            this.machine = machine;
            this.sysbus = machine.GetSystemBus(this);
            regs = new Dictionary<long, uint>();
            records = new List<Segment>();
            IRQ = new GPIO();
            Connections = new Dictionary<int, IGPIO> { { 0, IRQ } };
            dc = DataCommand.Undriven;
        }

        public GPIO IRQ { get; private set; }
        public IReadOnlyDictionary<int, IGPIO> Connections { get; private set; }

        // Data/command select (P1.09), driven by the display driver before each
        // transfer: low selects a controller opcode, high selects payload.
        public void OnGPIO(int number, bool value)
        {
            if(number != DataCommandPin)
            {
                return;
            }
            dc = value ? DataCommand.Data : DataCommand.Command;
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
            records.Clear();
            endFlag = false;
            inten = 0;
            dc = DataCommand.Undriven;
            warnedNoDataCommand = false;
        }

        private void DoTransfer()
        {
            uint txp = Get(TxPtr), txn = Get(TxCnt), rxp = Get(RxPtr), rxn = Get(RxCnt);
            if(dc == DataCommand.Undriven && !warnedNoDataCommand)
            {
                warnedNoDataCommand = true;
                this.Log(LogLevel.Error, "transfer with data/command never driven: P1.09 is not wired to this peripheral, the command stream cannot be separated from the pixel payload");
            }
            var tx = new byte[txn];
            for(uint i = 0; i < txn; i++)
            {
                tx[i] = sysbus.ReadByte((ulong)(txp + i));
            }
            records.Add(new Segment { Dc = dc, Data = tx });
            // display transfers are write-only; zero-fill the rx region if any
            for(uint i = 0; i < rxn; i++)
            {
                sysbus.WriteByte((ulong)(rxp + i), 0);
            }
            regs[TxAmount] = txn;
            regs[RxAmount] = rxn;
            endFlag = true;
            UpdateIrq();
        }

        // Raw dump: one line per DMA segment, "<C|D|?> <hex bytes>".
        public void DumpStream(string path)
        {
            using(var f = new StreamWriter(path))
            {
                foreach(var s in records)
                {
                    f.Write(Tag(s.Dc));
                    f.Write(' ');
                    foreach(var b in s.Data) f.Write(b.ToString("X2"));
                    f.WriteLine();
                }
            }
            this.Log(LogLevel.Info, "oled stream: {0} segments -> {1}", records.Count, path);
        }

        // Concatenate all data (D/C high) bytes and write them raw for offline
        // framebuffer reconstruction.
        public void DumpPixels(string path)
        {
            using(var f = new FileStream(path, FileMode.Create))
            {
                foreach(var s in records)
                {
                    if(s.Dc == DataCommand.Data)
                    {
                        f.Write(s.Data, 0, s.Data.Length);
                    }
                }
            }
            this.Log(LogLevel.Info, "oled pixels -> {0}", path);
        }

        // One file per bulk data segment: these are whole-frame pushes, and
        // keeping them apart avoids guessing frame boundaries offline.
        public void DumpFrames(string directory, int minimumLength)
        {
            Directory.CreateDirectory(directory);
            int index = 0;
            foreach(var s in records)
            {
                if(s.Dc != DataCommand.Data || s.Data.Length < minimumLength)
                {
                    continue;
                }
                var path = Path.Combine(directory, string.Format("frame{0:D3}.bin", index));
                File.WriteAllBytes(path, s.Data);
                index++;
            }
            this.Log(LogLevel.Info, "oled frames: {0} segments of >= {1} bytes -> {2}", index, minimumLength, directory);
        }

        private static string Tag(DataCommand value)
        {
            switch(value)
            {
            case DataCommand.Command:
                return "C";
            case DataCommand.Data:
                return "D";
            default:
                return "?";
            }
        }

        private uint Get(long off)
        {
            uint v;
            return regs.TryGetValue(off, out v) ? v : 0u;
        }

        public long Size { get { return 0x1000; } }

        private class Segment
        {
            public DataCommand Dc;
            public byte[] Data;
        }

        private const long TasksStart = 0x10;
        private const long EventsEnd = 0x118;
        private const long IntenSet = 0x304;
        private const long IntenClr = 0x308;
        private const uint EndIntBit = 1u << 6;
        private const long RxPtr = 0x534;
        private const long RxCnt = 0x538;
        private const long RxAmount = 0x53C;
        private const long TxPtr = 0x544;
        private const long TxCnt = 0x548;
        private const long TxAmount = 0x54C;

        private const int DataCommandPin = 9;

        private uint inten;
        private bool endFlag;
        private bool warnedNoDataCommand;
        private DataCommand dc;
        private readonly Dictionary<long, uint> regs;
        private readonly List<Segment> records;
        private readonly IMachine machine;
        private readonly IBusController sysbus;
    }
}
