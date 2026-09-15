//
// nRF52840 PWM. Renode 1.17 ships no model, so the SVD stub never raised
// EVENTS_SEQEND and the nrfx_pwm playback calls (vibration motor, display
// backlight) never finished. This is a completion model: a SEQSTART plays the
// whole sequence and all LOOP repetitions in zero time, so SEQSTARTED, SEQEND
// and LOOPSDONE all fire on the write.
//
using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Core.Structure;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    public class NrfPwm : IDoubleWordPeripheral, IKnownSize, INumberedGPIOOutput
    {
        public NrfPwm(IMachine machine)
        {
            this.machine = machine;
            this.sysbus = machine.GetSystemBus(this);
            regs = new Dictionary<long, uint>();
            IRQ = new GPIO();
            Connections = new Dictionary<int, IGPIO> { { 0, IRQ } };
            Reset();
        }

        public GPIO IRQ { get; private set; }
        public IReadOnlyDictionary<int, IGPIO> Connections { get; private set; }

        public uint ReadDoubleWord(long offset)
        {
            if(IsEvent(offset) || IsStored(offset))
            {
                return Get(offset);
            }
            if(offset == (long)Reg.Inten || offset == (long)Reg.IntenSet || offset == (long)Reg.IntenClr)
            {
                return inten;
            }
            this.Log(LogLevel.Warning, "Read from unhandled PWM offset 0x{0:X}", offset);
            return 0;
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            if(IsEvent(offset))
            {
                regs[offset] = value;
                UpdateIrq();
                return;
            }
            if(IsStored(offset))
            {
                regs[offset] = value;
                return;
            }
            switch((Reg)offset)
            {
            case Reg.TasksStop:
                regs[(long)Reg.EventsStopped] = 1;
                UpdateIrq();
                break;
            case Reg.TasksSeqStart0:
                PlaySequence(0);
                break;
            case Reg.TasksSeqStart1:
                PlaySequence(1);
                break;
            case Reg.TasksNextStep:
                regs[(long)Reg.EventsPwmPeriodEnd] = 1;
                UpdateIrq();
                break;
            case Reg.Inten:
                inten = value;
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
                this.Log(LogLevel.Warning, "Write 0x{0:X} to unhandled PWM offset 0x{1:X}", value, offset);
                break;
            }
        }

        private void PlaySequence(int sequence)
        {
            LogFirstDutyValue(sequence);
            regs[(long)Reg.EventsSeqStarted0 + 4 * sequence] = 1;
            regs[(long)Reg.EventsSeqEnd0 + 4 * sequence] = 1;
            regs[(long)Reg.EventsLoopsDone] = 1;
            UpdateIrq();
        }

        // The only observable output of a model that simulates no timing: which
        // pins a sequence drives and how hard, so a run can tell the vibration
        // motor apart from the backlight.
        private void LogFirstDutyValue(int sequence)
        {
            var pointer = Get(SeqBase + SeqStride * sequence);
            var count = Get(SeqBase + SeqStride * sequence + 4);
            if(pointer == 0 || count == 0)
            {
                this.Log(LogLevel.Warning, "SEQSTART[{0}] with PTR=0x{1:X} CNT={2}, nothing to play", sequence, pointer, count);
                return;
            }
            this.Log(LogLevel.Debug, "SEQSTART[{0}] duty=0x{1:X4} cnt={2} loop={3} countertop={4} pins={5},{6},{7},{8}",
                sequence, sysbus.ReadWord(pointer), count, Get((long)Reg.Loop), Get((long)Reg.CounterTop),
                Get((long)Reg.PselOut0), Get((long)Reg.PselOut0 + 4), Get((long)Reg.PselOut0 + 8), Get((long)Reg.PselOut0 + 12));
        }

        private void UpdateIrq()
        {
            uint pending = 0;
            if(Get((long)Reg.EventsStopped) != 0) pending |= 1u << 1;
            if(Get((long)Reg.EventsSeqStarted0) != 0) pending |= 1u << 2;
            if(Get((long)Reg.EventsSeqStarted1) != 0) pending |= 1u << 3;
            if(Get((long)Reg.EventsSeqEnd0) != 0) pending |= 1u << 4;
            if(Get((long)Reg.EventsSeqEnd1) != 0) pending |= 1u << 5;
            if(Get((long)Reg.EventsPwmPeriodEnd) != 0) pending |= 1u << 6;
            if(Get((long)Reg.EventsLoopsDone) != 0) pending |= 1u << 7;
            IRQ.Set((pending & inten) != 0);
        }

        private bool IsEvent(long offset)
        {
            return offset >= (long)Reg.EventsStopped && offset <= (long)Reg.EventsLoopsDone && (offset & 3) == 0;
        }

        private bool IsStored(long offset)
        {
            if(offset == (long)Reg.Shorts)
            {
                return true;
            }
            if(offset >= (long)Reg.Enable && offset <= (long)Reg.Loop && (offset & 3) == 0)
            {
                return true;
            }
            if(offset >= SeqBase && offset < SeqBase + SeqStride * SequenceCount && (offset & 3) == 0)
            {
                return true;
            }
            return offset >= (long)Reg.PselOut0 && offset <= (long)Reg.PselOut0 + 12 && (offset & 3) == 0;
        }

        public void Reset()
        {
            regs.Clear();
            inten = 0;
            IRQ.Set(false);
        }

        private uint Get(long offset)
        {
            uint v;
            return regs.TryGetValue(offset, out v) ? v : 0u;
        }

        public long Size { get { return 0x1000; } }

        private const long SeqBase = 0x520;
        private const long SeqStride = 0x20;
        private const int SequenceCount = 2;

        private enum Reg : long
        {
            TasksStop = 0x004,
            TasksSeqStart0 = 0x008,
            TasksSeqStart1 = 0x00C,
            TasksNextStep = 0x010,
            EventsStopped = 0x104,
            EventsSeqStarted0 = 0x108,
            EventsSeqStarted1 = 0x10C,
            EventsSeqEnd0 = 0x110,
            EventsSeqEnd1 = 0x114,
            EventsPwmPeriodEnd = 0x118,
            EventsLoopsDone = 0x11C,
            Shorts = 0x200,
            Inten = 0x300,
            IntenSet = 0x304,
            IntenClr = 0x308,
            Enable = 0x500,
            Mode = 0x504,
            CounterTop = 0x508,
            Prescaler = 0x50C,
            Decoder = 0x510,
            Loop = 0x514,
            PselOut0 = 0x560,
        }

        private uint inten;
        private readonly Dictionary<long, uint> regs;
        private readonly IBusController sysbus;
        private readonly IMachine machine;
    }
}
