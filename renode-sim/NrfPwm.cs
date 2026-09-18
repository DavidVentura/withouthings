//
// nRF52840 PWM. Renode 1.17 ships no model, so the SVD stub never raised
// EVENTS_SEQEND and the nrfx_pwm playback calls (vibration motor, display
// backlight, the three step motors) never finished.
//
// A playback consumes the virtual time its sequence describes: a period is
// COUNTERTOP ticks of the prescaled 16 MHz clock, an entry is played REFRESH+1
// times, and a playback walks SEQ0 then SEQ1 once per LOOP. SEQEND[n],
// LOOPSDONE and the STOPPED the shortcuts ask for fall at the times they fall.
// With the completion model that raised all of them on the SEQSTART write the
// step rate of a hand was set by how many instructions the CPU retired between
// steps, so any change to the code in between moved every later deadline.
//
using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Core.Structure;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Time;

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

        // Set on the instances a step motor hangs off, which decode the played
        // sequence into hand movement.
        public NrfHands Hands { get; set; }

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
                StopPlayback();
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

        // Every milestone of the playback is booked here, at the virtual time
        // it falls: a booking made from inside another booking's callback never
        // comes due, and the times are all known from the registers anyway. A
        // booking that comes due after TASKS_STOP, or after a shortcut ended
        // the playback early, is a booking of an older generation and does
        // nothing.
        private void PlaySequence(int sequence)
        {
            StopPlayback();
            LogFirstDutyValue(sequence);
            step = ReadStep();
            regs[(long)Reg.EventsSeqStarted0 + 4 * sequence] = 1;
            UpdateIrq();

            var loop = Get((long)Reg.Loop) & 0xFFFF;
            var plays = loop == 0 ? 1 : 2 * (int)loop;
            var played = new List<Milestone>();
            ulong at = 0;
            for(var i = 0; i < plays; i++)
            {
                var seq = (sequence + i) % SequenceCount;
                var duration = SequenceDuration(seq);
                if(duration == 0)
                {
                    continue;
                }
                at += duration;
                var hands = played.Count == 0 && seq == 0 && step != null;
                played.Add(new Milestone(at, seq, hands));
            }
            if(played.Count == 0)
            {
                Reach(new Milestone(0, LoopsDoneOnly, step != null).AsLast());
                return;
            }
            played[played.Count - 1] = played[played.Count - 1].AsLast();
            var booked = generation;
            foreach(var milestone in played)
            {
                var due = milestone;
                machine.ScheduleAction(TimeInterval.FromMicroseconds(Math.Max(1, due.At / 1000)),
                                       _ => { if(booked == generation) Reach(due); });
            }
        }

        private void Reach(Milestone milestone)
        {
            if(milestone.DeliversStep)
            {
                DeliverStep();
            }
            var shorts = Get((long)Reg.Shorts);
            if(milestone.Sequence != LoopsDoneOnly)
            {
                regs[(long)Reg.EventsSeqEnd0 + 4 * milestone.Sequence] = 1;
                var stop = milestone.Sequence == 0 ? SeqEndStop0 : SeqEndStop1;
                if((shorts & stop) != 0)
                {
                    StopPlayback();
                    regs[(long)Reg.EventsStopped] = 1;
                    UpdateIrq();
                    return;
                }
            }
            if(!milestone.Last)
            {
                UpdateIrq();
                return;
            }
            regs[(long)Reg.EventsLoopsDone] = 1;
            if((shorts & LoopsDoneStop) != 0)
            {
                regs[(long)Reg.EventsStopped] = 1;
            }
            if((shorts & (LoopsDoneSeqStart0 | LoopsDoneSeqStart1)) != 0)
            {
                this.Log(LogLevel.Warning, "SHORTS 0x{0:X} chains another sequence, which this model does not replay", shorts);
            }
            UpdateIrq();
        }

        private void StopPlayback()
        {
            generation++;
            step = null;
        }

        // In nanoseconds, because a period is COUNTERTOP ticks of
        // 16 MHz / 2^PRESCALER, which is 62.5 ns a tick at PRESCALER 0.
        private ulong SequenceDuration(int seq)
        {
            var pointer = Get(SeqBase + SeqStride * seq);
            var count = Get(SeqBase + SeqStride * seq + 4);
            if(pointer == 0 || count == 0)
            {
                return 0;
            }
            var refresh = (Get(SeqBase + SeqStride * seq + 8) & 0xFFFFFF) + 1;
            var prescaler = Get((long)Reg.Prescaler) & 0x7;
            var load = Get((long)Reg.Decoder) & DecoderLoadMask;
            var waveForm = load == DecoderLoadWaveForm;
            // DECODER.LOAD says how many of the halfwords SEQ.CNT counts make
            // one period: one in Common, two in Grouped, four in Individual
            // and WaveForm.
            var perPeriod = load == DecoderLoadGrouped ? 2u : load >= DecoderLoadIndividual ? 4u : 1u;
            var entries = count / perPeriod;
            ulong ticks = 0;
            for(var entry = 0u; entry < entries; entry++)
            {
                ticks += waveForm
                    ? sysbus.ReadWord(pointer + 2 * (entry * perPeriod + 3))
                    : Get((long)Reg.CounterTop);
            }
            return ticks * refresh * (1ul << (int)prescaler) * 125 / 2;
        }

        // Which pins a sequence drives and how hard, so a run can tell the
        // vibration motor apart from the backlight.
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

        // DECODER.LOAD = WaveForm: an entry is four halfwords, three channel
        // compares and a COUNTERTOP that replaces the register for the period.
        // The entry is read when the playback starts and handed over when the
        // drive pulse it describes has been played.
        private ushort[] ReadStep()
        {
            var pointer = Get(SeqBase);
            if(Hands == null || pointer == 0 || Get(SeqBase + 4) != WaveFormEntryLength
               || (Get((long)Reg.Decoder) & DecoderLoadMask) != DecoderLoadWaveForm)
            {
                return null;
            }
            return new[]
            {
                sysbus.ReadWord(pointer), sysbus.ReadWord(pointer + 2),
                sysbus.ReadWord(pointer + 4), sysbus.ReadWord(pointer + 6),
            };
        }

        private void DeliverStep()
        {
            var played = step;
            step = null;
            Hands.PlayedSequence(Get((long)Reg.PselOut0), Get((long)Reg.PselOut0 + 4), Get((long)Reg.PselOut0 + 8),
                                 played[0], played[1], played[2], played[3]);
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
            StopPlayback();
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

        private struct Milestone
        {
            public Milestone(ulong at, int sequence, bool deliversStep)
                : this(at, sequence, deliversStep, false)
            {
            }

            private Milestone(ulong at, int sequence, bool deliversStep, bool last)
            {
                At = at;
                Sequence = sequence;
                DeliversStep = deliversStep;
                Last = last;
            }

            // The last milestone of a playback is also its LOOPSDONE.
            public Milestone AsLast()
            {
                return new Milestone(At, Sequence, DeliversStep, true);
            }

            public readonly ulong At;
            public readonly int Sequence;
            public readonly bool DeliversStep;
            public readonly bool Last;
        }

        // A playback whose sequences are all empty is LOOPSDONE and nothing else.
        private const int LoopsDoneOnly = -1;
        private const uint SeqEndStop0 = 1u << 0;
        private const uint SeqEndStop1 = 1u << 1;
        private const uint LoopsDoneSeqStart0 = 1u << 2;
        private const uint LoopsDoneSeqStart1 = 1u << 3;
        private const uint LoopsDoneStop = 1u << 4;
        private const uint WaveFormEntryLength = 4;
        private const uint DecoderLoadMask = 0x7;
        private const uint DecoderLoadGrouped = 1;
        private const uint DecoderLoadIndividual = 2;
        private const uint DecoderLoadWaveForm = 3;
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
        private int generation;
        private ushort[] step;
        private readonly Dictionary<long, uint> regs;
        private readonly IBusController sysbus;
        private readonly IMachine machine;
    }
}
