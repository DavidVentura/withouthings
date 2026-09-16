//
// A WPP link into the simulated watch with no radio on either side.
//
// The SoftDevice is running but has no peer, so the pipe is spliced at the
// application's boundary with it rather than at the radio:
//
//   inbound   the application polls sd_ble_evt_get in its own task and
//             dispatches whatever it gets. A hook on the instruction after
//             that call writes a synthesised ble_evt_t into the buffer the
//             application already passed and reports NRF_SUCCESS, so the event
//             is built by us but queued, owned and freed by nobody: the
//             SoftDevice's queue is never touched. The task is woken by
//             pending its own SD-event IRQ. Everything runs on the task and
//             stack the real event would have run on, so no critical region,
//             BASEPRI or re-entrancy state has to be reasoned about.
//
//   outbound  a hook on the application's sd_ble_gatts_hvx thunk reads the
//             hvx params, captures the payload and returns NRF_SUCCESS from
//             the thunk without executing the SVC. The SoftDevice would fail
//             the call: it knows of no connection.
//
// Framing on the TCP socket, both directions: [len u16 big-endian][payload].
// One inbound message is one GATT write to the WPP characteristic, one
// outbound message is one notification. WPP's own reassembly, in both the
// firmware and the client, sees the same byte stream it would over a real
// link.
//
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.CPU;
using Antmicro.Renode.Time;
using Antmicro.Renode.Utilities;

namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    public class WppPipe : IPeripheral, IDisposable
    {
        public WppPipe(IMachine machine, int port, ulong evtPresentHook, ulong evtPollHook, ulong evtBuffer,
                       ulong hvxThunk, ulong wppServiceContext, int sdEventIrq)
        {
            this.evtPresentHook = evtPresentHook;
            this.machine = machine;
            this.port = port;
            this.evtPollHook = evtPollHook;
            this.evtBuffer = evtBuffer;
            this.hvxThunk = hvxThunk;
            this.wppServiceContext = wppServiceContext;
            this.sdEventIrq = sdEventIrq;
            inbound = new ConcurrentQueue<byte[]>();
        }

        // Installing the hooks needs the CPU, which does not exist yet while the
        // platform description is being parsed, so this is a separate step the
        // run script takes once the machine is up.
        public void Attach()
        {
            if(cpu != null)
            {
                throw new RecoverableException("the pipe is already attached");
            }
            cpu = machine.SystemBus.GetCPUs().OfType<ICPUWithHooks>().Single();
            cpu.AddHook(evtPresentHook, OnEventPresenceCheck);
            cpu.AddHook(evtPollHook, OnEventPoll);
            cpu.AddHook(hvxThunk, OnHvx);
            listener = new TcpListener(IPAddress.Loopback, port);
            listener.Start();
            worker = new Thread(Serve) { IsBackground = true, Name = "wpp-pipe" };
            worker.Start();
            this.Log(LogLevel.Info, "WPP pipe listening on 127.0.0.1:{0}", port);
        }

        public void Reset()
        {
        }

        public void Dispose()
        {
            running = false;
            if(listener != null)
            {
                listener.Stop();
            }
        }

        private void Serve()
        {
            while(running)
            {
                TcpClient client;
                try
                {
                    client = listener.AcceptTcpClient();
                }
                catch(SocketException)
                {
                    return; // the listener was stopped
                }
                using(client)
                {
                    client.NoDelay = true;
                    this.Log(LogLevel.Info, "WPP client connected");
                    var queue = new BlockingCollection<byte[]>(new ConcurrentQueue<byte[]>());
                    outbound = queue;
                    Link();
                    var reader = new Thread(() => Drain(client, queue)) { IsBackground = true, Name = "wpp-pipe-tx" };
                    reader.Start();
                    try
                    {
                        Receive(client.GetStream());
                    }
                    catch(IOException)
                    {
                    }
                    Unlink();
                    outbound = null;
                    queue.CompleteAdding();
                    this.Log(LogLevel.Info, "WPP client gone");
                }
            }
        }

        private void Receive(NetworkStream stream)
        {
            var header = new byte[2];
            while(running)
            {
                if(!ReadExactly(stream, header, 2))
                {
                    return;
                }
                var length = (header[0] << 8) | header[1];
                if(length == 0 || length > MaxWriteLength)
                {
                    throw new IOException(string.Format("inbound frame of {0} bytes, the characteristic takes 1..{1}",
                                                        length, MaxWriteLength));
                }
                var payload = new byte[length];
                if(!ReadExactly(stream, payload, length))
                {
                    return;
                }
                Post(BuildWrite(WriteHandleOffset, payload));
            }
        }

        private void Drain(TcpClient client, BlockingCollection<byte[]> queue)
        {
            var stream = client.GetStream();
            try
            {
                foreach(var notification in queue.GetConsumingEnumerable())
                {
                    var framed = new byte[notification.Length + 2];
                    framed[0] = (byte)(notification.Length >> 8);
                    framed[1] = (byte)notification.Length;
                    Array.Copy(notification, 0, framed, 2, notification.Length);
                    stream.Write(framed, 0, framed.Length);
                    stream.Flush();
                }
            }
            catch(IOException)
            {
            }
            catch(ObjectDisposedException)
            {
            }
        }

        private static bool ReadExactly(NetworkStream stream, byte[] buffer, int count)
        {
            var got = 0;
            while(got < count)
            {
                var read = stream.Read(buffer, got, count - got);
                if(read == 0)
                {
                    return false;
                }
                got += read;
            }
            return true;
        }

        // A TCP connection stands for the BLE one: the firmware only accepts WPP
        // bytes once a link is up and the client has subscribed to notifications,
        // so the two events a phone would cause are synthesised here.
        private void Link()
        {
            while(inbound.TryDequeue(out var _))
            {
            }
            Post(BuildConnected());
            Post(BuildWrite(CccdHandleOffset, new byte[] { 0x01, 0x00 }));
        }

        private void Unlink()
        {
            Post(BuildDisconnected());
        }

        private void Post(Func<byte[]> build)
        {
            inbound.Enqueue(build());
            machine.HandleTimeDomainEvent(PendSdEvent, 0, TimeDomainsManager.Instance.GetEffectiveVirtualTimeStamp());
        }

        private void PendSdEvent(int unused)
        {
            machine.SystemBus.WriteDoubleWord(NvicSetPending, 1u << sdEventIrq);
        }

        private byte[] Event(ushort id, byte[] body)
        {
            // ble_evt_t: [evt_id u16][evt_len u16][conn_handle u16][params...],
            // where evt_len counts from the connection handle on.
            var evt = new byte[6 + body.Length];
            evt[0] = (byte)id;
            evt[1] = (byte)(id >> 8);
            var length = (ushort)(2 + body.Length);
            evt[2] = (byte)length;
            evt[3] = (byte)(length >> 8);
            evt[4] = (byte)ConnectionHandle;
            evt[5] = (byte)(ConnectionHandle >> 8);
            Array.Copy(body, 0, evt, 6, body.Length);
            return evt;
        }

        private Func<byte[]> BuildConnected()
        {
            return () =>
            {
                // ble_gap_evt_connected_t: peer_addr(7), role(1), conn_params(8).
                var body = new byte[16];
                body[0] = 0x02; // addr_type = RANDOM_STATIC, addr_id_peer = 0
                Array.Copy(PeerAddress, 0, body, 1, 6);
                body[7] = 0x01; // BLE_GAP_ROLE_PERIPH
                Write16(body, 8, 12); // min interval, 1.25 ms units
                Write16(body, 10, 12); // max interval
                Write16(body, 12, 30); // slave latency, as the watch asks for
                Write16(body, 14, 400); // supervision timeout, 10 ms units
                return Event(GapEvtConnected, body);
            };
        }

        private Func<byte[]> BuildDisconnected()
        {
            return () => Event(GapEvtDisconnected, new byte[] { 0x08 }); // REMOTE_USER_TERMINATED
        }

        // The value and CCCD handles are whatever the SoftDevice assigned when the
        // firmware registered the service, so they are read back out of the
        // firmware's own service context at the moment the event is built.
        private Func<byte[]> BuildWrite(ulong handleOffset, byte[] payload)
        {
            return () =>
            {
                var handle = machine.SystemBus.ReadWord(wppServiceContext + handleOffset);
                // ble_gatts_evt_write_t: handle(2), uuid(4), op(1), auth_required(1),
                // offset(2), len(2), data[].
                var body = new byte[12 + payload.Length];
                Write16(body, 0, handle);
                Write16(body, 2, WppCharacteristicUuid);
                body[4] = VendorUuidType;
                body[6] = 0x02; // BLE_GATTS_OP_WRITE_CMD, as the phone writes
                Write16(body, 10, (ushort)payload.Length);
                Array.Copy(payload, 0, body, 12, payload.Length);
                return Event(GattsEvtWrite, body);
            };
        }

        private static void Write16(byte[] buffer, int offset, ushort value)
        {
            buffer[offset] = (byte)value;
            buffer[offset + 1] = (byte)(value >> 8);
        }

        // The SD-event ISR only asks whether an event is waiting and wakes the
        // task that will fetch it; with a null destination the SoftDevice answers
        // NRF_ERROR_DATA_SIZE when there is one and NRF_ERROR_NOT_FOUND when there
        // is not, so a queued injection has to answer the question too or the task
        // is never woken.
        private void OnEventPresenceCheck(ICpuSupportingGdb source, ulong address)
        {
            if(inbound.TryPeek(out var _))
            {
                source.SetRegisterUlong(ResultRegister, NrfErrorDataSize);
            }
        }

        private void OnEventPoll(ICpuSupportingGdb source, ulong address)
        {
            if(!inbound.TryDequeue(out var evt))
            {
                return; // let the real result through, which is NRF_ERROR_NOT_FOUND
            }
            if(evt.Length > EventBufferLength)
            {
                throw new RecoverableException(string.Format("a {0} byte event does not fit the firmware's {1} byte buffer",
                                                             evt.Length, EventBufferLength));
            }
            machine.SystemBus.WriteBytes(evt, evtBuffer, true);
            source.SetRegisterUlong(ResultRegister, NrfSuccess);
            this.Log(LogLevel.Debug, "injected ble_evt id=0x{0:x2} len={1}", evt[0], evt.Length);
        }

        private void OnHvx(ICpuSupportingGdb source, ulong address)
        {
            var parameters = source.GetRegisterUlong(SecondArgumentRegister);
            // ble_gatts_hvx_params_t: handle(2), type(1), offset(2), p_len(4), p_data(4)
            var length = machine.SystemBus.ReadWord((ulong)machine.SystemBus.ReadDoubleWord(parameters + 8));
            var data = machine.SystemBus.ReadBytes(machine.SystemBus.ReadDoubleWord(parameters + 12), length, true);
            var queue = outbound;
            if(queue == null)
            {
                this.Log(LogLevel.Warning, "notification with no client connected, dropped ({0} bytes)", length);
            }
            else
            {
                queue.Add(data);
            }
            // The firmware counts notifications in flight and waits for the SoftDevice's
            // HVN_TX_COMPLETE before queueing more; without it a reply longer than the
            // queue depth stalls forever.
            Post(() => Event(GattsEvtHvnTxComplete, new byte[] { 0x01, 0x00 }));
            this.Log(LogLevel.Debug, "captured hvx handle=0x{0:x4} type={1} len={2}",
                     machine.SystemBus.ReadWord(parameters), machine.SystemBus.ReadByte(parameters + 2), length);
            // Return NRF_SUCCESS instead of running the SVC: the SoftDevice has no
            // connection to notify on and would reject the handle.
            source.SetRegisterUlong(ResultRegister, NrfSuccess);
            source.PC = address + 2;
        }

        // Cortex-M register indices in Renode's ARM register enumeration.
        private const int ResultRegister = 0;
        private const int SecondArgumentRegister = 1;

        private const ulong NrfSuccess = 0;
        private const ulong NrfErrorDataSize = 0x0b;
        private const ulong NvicSetPending = 0xE000E200;
        private const ushort ConnectionHandle = 0;
        private const ushort GapEvtConnected = 0x10;
        private const ushort GapEvtDisconnected = 0x11;
        private const ushort GattsEvtWrite = 0x50;
        private const ushort GattsEvtHvnTxComplete = 0x57;
        private const ushort WppCharacteristicUuid = 0x0001;
        private const byte VendorUuidType = 0x02;
        private const int EventBufferLength = 0xec;
        private const int MaxWriteLength = EventBufferLength - 18;
        // The service context holds a value and a CCCD handle per characteristic:
        // WPP_V2 at +0x04/+0x08, WPP_V3 at +0x0c/+0x10, WPPS at +0x14/+0x18.
        // Subscribing to WPPS puts the firmware into its TLS record protocol, so
        // the pipe uses WPP_V2, which carries WPP frames as they are.
        private const ulong WriteHandleOffset = 0x04;
        private const ulong CccdHandleOffset = 0x08;
        private static readonly byte[] PeerAddress = new byte[] { 0x01, 0x02, 0x03, 0x04, 0x05, 0xc0 };

        private ICPUWithHooks cpu;
        private TcpListener listener;
        private Thread worker;
        private volatile bool running = true;

        private readonly IMachine machine;
        private readonly int port;
        private readonly ulong evtPresentHook;
        private readonly ulong evtPollHook;
        private readonly ulong evtBuffer;
        private readonly ulong hvxThunk;
        private readonly ulong wppServiceContext;
        private readonly int sdEventIrq;
        private readonly ConcurrentQueue<byte[]> inbound;
        private volatile BlockingCollection<byte[]> outbound;
    }
}
