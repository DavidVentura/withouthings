//
// nRF52840 NVMC. Renode 1.17 ships no model, so the block fell through to the
// SVD stub whose READY reads 0 and every nrfx_nvmc call span forever. This is a
// completion model: the flash is a MappedMemory with no programming latency, so
// READY/READYNEXT are always 1.
//
// Why writes are not filtered: a word write with CONFIG=WEN reaches the
// MappedMemory directly and never passes through this peripheral, so the NOR
// "can only clear bits" rule is not enforced. Firmware that programs the same
// word twice would see a result the silicon could not produce.
//
using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.MTD
{
    public class NrfNvmc : IDoubleWordPeripheral, IKnownSize
    {
        public NrfNvmc(IMachine machine)
        {
            this.machine = machine;
            this.sysbus = machine.GetSystemBus(this);
            regs = new Dictionary<long, uint>();
            Reset();
        }

        public uint ReadDoubleWord(long offset)
        {
            switch((Reg)offset)
            {
            case Reg.Ready:
            case Reg.ReadyNext:
                return 1;
            case Reg.Config:
            case Reg.ErasePagePartialCfg:
            case Reg.IcacheCnf:
            case Reg.IHit:
            case Reg.IMiss:
                return Get(offset);
            default:
                this.Log(LogLevel.Warning, "Read from unhandled NVMC offset 0x{0:X}", offset);
                return 0;
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            switch((Reg)offset)
            {
            case Reg.Config:
                regs[offset] = value & 0x3;
                break;
            case Reg.ErasePage:
            case Reg.ErasePagePartial:
                ErasePage(value);
                break;
            case Reg.EraseAll:
                this.Log(LogLevel.Warning, "ERASEALL ignored: it would wipe the loaded image and the boot patches");
                break;
            case Reg.EraseUicr:
                this.Log(LogLevel.Warning, "ERASEUICR ignored: the UICR is an SVD tag, not writable memory");
                break;
            case Reg.ErasePagePartialCfg:
            case Reg.IcacheCnf:
            case Reg.IHit:
            case Reg.IMiss:
                regs[offset] = value;
                break;
            default:
                this.Log(LogLevel.Warning, "Write 0x{0:X} to unhandled NVMC offset 0x{1:X}", value, offset);
                break;
            }
        }

        private void ErasePage(uint address)
        {
            if((Mode)Get((long)Reg.Config) != Mode.EraseEnable)
            {
                this.Log(LogLevel.Error, "ERASEPAGE 0x{0:X} with CONFIG={1}, erase not enabled; ignoring", address, (Mode)Get((long)Reg.Config));
                return;
            }
            var page = address & ~(PageSize - 1);
            for(uint i = 0; i < PageSize; i += 4)
            {
                sysbus.WriteDoubleWord(page + i, 0xFFFFFFFF);
            }
            this.Log(LogLevel.Debug, "Erased flash page at 0x{0:X}", page);
        }

        // The MBR and SoftDevice never program their own region, so a stray CPU
        // store that the silicon's read-only NVMC would drop shows up here as a
        // difference from the loaded image. Called from the run script, not Reset:
        // a SYSRESETREQ must not discard the reference.
        public void SnapshotProtectedRegion(ulong start, ulong size)
        {
            protectedStart = start;
            protectedImage = sysbus.ReadBytes(start, (int)size);
            this.Log(LogLevel.Info, "protected region snapshot: 0x{0:X}..0x{1:X}", start, start + size);
        }

        public uint VerifyProtectedRegion()
        {
            if(protectedImage == null)
            {
                throw new RecoverableException("no snapshot: call SnapshotProtectedRegion first");
            }
            var now = sysbus.ReadBytes(protectedStart, protectedImage.Length);
            uint differing = 0;
            var first = new List<string>();
            for(var i = 0; i < now.Length; i += 4)
            {
                if(now[i] == protectedImage[i] && now[i + 1] == protectedImage[i + 1] && now[i + 2] == protectedImage[i + 2] && now[i + 3] == protectedImage[i + 3])
                {
                    continue;
                }
                differing++;
                if(first.Count < 8)
                {
                    first.Add(string.Format("0x{0:X}", protectedStart + (ulong)i));
                }
            }
            if(differing == 0)
            {
                this.Log(LogLevel.Info, "protected region intact");
                return 0;
            }
            this.Log(LogLevel.Error, "protected region changed: {0} words differ from the loaded image, first at {1}", differing, string.Join(" ", first));
            return differing;
        }

        public void Reset()
        {
            regs.Clear();
        }

        private uint Get(long offset)
        {
            uint v;
            return regs.TryGetValue(offset, out v) ? v : 0u;
        }

        public long Size { get { return 0x1000; } }

        private const uint PageSize = 0x1000;

        private enum Reg : long
        {
            Ready = 0x400,
            ReadyNext = 0x408,
            Config = 0x504,
            ErasePage = 0x508,
            EraseAll = 0x50C,
            EraseUicr = 0x514,
            ErasePagePartial = 0x518,
            ErasePagePartialCfg = 0x51C,
            IcacheCnf = 0x540,
            IHit = 0x548,
            IMiss = 0x54C,
        }

        private enum Mode : uint
        {
            ReadOnly = 0,
            WriteEnable = 1,
            EraseEnable = 2,
        }

        private byte[] protectedImage;
        private ulong protectedStart;
        private readonly Dictionary<long, uint> regs;
        private readonly IBusController sysbus;
        private readonly IMachine machine;
    }
}
