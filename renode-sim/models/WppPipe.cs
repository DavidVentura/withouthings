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
// ANCS rides a second listener, because on that service the roles are the other
// way round: the watch is the GATT client and the phone the server, so nothing
// about the WPP framing above describes it and folding the two onto one socket
// would have cost the WPP client a byte it does not send today. The ANCS socket
// frames the same way, [len u16 big-endian][payload], with payload[0] a kind:
//
//   0x01  host -> sim   the eight byte notification-source value to notify
//   0x02  sim  -> host  the control-point write the watch just made
//   0x03  host -> sim   one data-source fragment to notify
//   0x04  sim  -> host  the watch has subscribed to both sources
//
// Everything between those three messages is GATT, and the pipe plays it. The
// watch's sd_ble_gattc_* calls are hooked the way sd_ble_gatts_hvx already is:
// the SVC never runs -- the SoftDevice knows of no peer and would reject it --
// and the pipe answers with the BLE_GATTC_EVT_* the SoftDevice would have
// delivered, queued through the same injection path as every other event.
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
        public WppPipe(IMachine machine, int port, ulong evtBuffer, ulong wppServiceContext,
                       int sdEventIrq, int ancsPort, ulong pairingRecord, ulong ancsUuidTable)
        {
            this.machine = machine;
            this.port = port;
            this.evtBuffer = evtBuffer;
            this.wppServiceContext = wppServiceContext;
            this.sdEventIrq = sdEventIrq;
            this.ancsPort = ancsPort;
            this.pairingRecord = pairingRecord;
            this.ancsUuidTable = ancsUuidTable;
            inbound = new ConcurrentQueue<byte[]>();
            pending = new Dictionary<ushort, List<byte>>();
        }

        // The three hook sites are addresses in the application image, so they
        // belong to the image that is running rather than to the platform: the
        // run script passes what abi/rig.py resolved them to. Installing them
        // also needs the CPU, which does not exist yet while the platform
        // description is being parsed. Attaching again moves the hooks, which
        // is what the update run does once the bootloader has replaced the
        // application with one whose text is laid out differently.
        public void Attach(ulong evtPresentHook, ulong evtPollHook, ulong hvxThunk)
        {
            if(cpu == null)
            {
                cpu = machine.SystemBus.GetCPUs().OfType<ICPUWithHooks>().Single();
                listener = new TcpListener(IPAddress.Loopback, port);
                listener.Start();
                worker = new Thread(Serve) { IsBackground = true, Name = "wpp-pipe" };
                worker.Start();
                this.Log(LogLevel.Info, "WPP pipe listening on 127.0.0.1:{0}", port);
            }
            else
            {
                foreach(var old in new[] { present, poll, hvx0 })
                {
                    cpu.RemoveHooksAt(old);
                }
            }
            present = evtPresentHook;
            poll = evtPollHook;
            hvx0 = hvxThunk;
            cpu.AddHook(present, OnEventPresenceCheck);
            cpu.AddHook(poll, OnEventPoll);
            cpu.AddHook(hvx0, OnHvx);
            this.Log(LogLevel.Info, "hooks at {0:X}, {1:X}, {2:X}", present, poll, hvx0);
        }

        // The ANCS side is attached separately from the WPP one because a run
        // that only wants WPP -- the update run does -- should not have the
        // watch's GATT client answered at all: with no ANCS server it discovers
        // nothing and stays where it is today.
        public void AttachAncs(ulong primaryServiceDiscoverThunk, ulong characteristicsDiscoverThunk,
                               ulong descriptorsDiscoverThunk, ulong writeThunk)
        {
            if(ancsListener == null)
            {
                ancsListener = new TcpListener(IPAddress.Loopback, ancsPort);
                ancsListener.Start();
                ancsWorker = new Thread(ServeAncs) { IsBackground = true, Name = "ancs-pipe" };
                ancsWorker.Start();
                this.Log(LogLevel.Info, "ANCS pipe listening on 127.0.0.1:{0}", ancsPort);
            }
            else
            {
                foreach(var old in new[] { discoverServices, discoverCharacteristics, discoverDescriptors, gattcWrite })
                {
                    cpu.RemoveHooksAt(old);
                }
            }
            discoverServices = primaryServiceDiscoverThunk;
            discoverCharacteristics = characteristicsDiscoverThunk;
            discoverDescriptors = descriptorsDiscoverThunk;
            gattcWrite = writeThunk;
            cpu.AddHook(discoverServices, OnPrimaryServicesDiscover);
            cpu.AddHook(discoverCharacteristics, OnCharacteristicsDiscover);
            cpu.AddHook(discoverDescriptors, OnDescriptorsDiscover);
            cpu.AddHook(gattcWrite, OnGattcWrite);
            this.Log(LogLevel.Info, "gattc hooks at {0:X}, {1:X}, {2:X}, {3:X}",
                     discoverServices, discoverCharacteristics, discoverDescriptors, gattcWrite);
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
            if(ancsListener != null)
            {
                ancsListener.Stop();
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

        // The ANCS socket. A connection stands for a phone that has bonded and
        // turned notifications on, so it also marks the link bonded: the ANCS
        // module refuses to discover anything for a peer the pairing module
        // does not hold a bond for, and in a run with no radio nothing ever
        // pairs.
        private void ServeAncs()
        {
            while(running)
            {
                TcpClient client;
                try
                {
                    client = ancsListener.AcceptTcpClient();
                }
                catch(SocketException)
                {
                    return; // the listener was stopped
                }
                using(client)
                {
                    client.NoDelay = true;
                    this.Log(LogLevel.Info, "ANCS client connected");
                    var queue = new BlockingCollection<byte[]>(new ConcurrentQueue<byte[]>());
                    ancsOutbound = queue;
                    subscribed[0] = false;
                    subscribed[1] = false;
                    machine.HandleTimeDomainEvent(Bond, 0, TimeDomainsManager.Instance.GetEffectiveVirtualTimeStamp());
                    var writer = new Thread(() => Drain(client, queue)) { IsBackground = true, Name = "ancs-pipe-tx" };
                    writer.Start();
                    try
                    {
                        ReceiveAncs(client.GetStream());
                    }
                    catch(IOException)
                    {
                    }
                    ancsOutbound = null;
                    queue.CompleteAdding();
                    this.Log(LogLevel.Info, "ANCS client gone");
                }
            }
        }

        // The pairing module's live peer record: byte 0 is the state the ANCS
        // guard reads through 0x41348 -- 1 means "bonded peer on the link" --
        // and the bonded address sits at +0x0a. Writing it is the same kind of
        // claim the injected BLE_GAP_EVT_CONNECTED already makes: there is no
        // peer, so there is nobody to bond with, and the firmware is told the
        // outcome rather than walked through the procedure.
        private void Bond(int unused)
        {
            machine.SystemBus.WriteBytes(PeerAddress, pairingRecord + BondedAddressOffset, true);
            machine.SystemBus.WriteByte(pairingRecord, 1);
            this.Log(LogLevel.Info, "marked the link bonded in the pairing record at 0x{0:x8}", pairingRecord);
        }

        private void ReceiveAncs(NetworkStream stream)
        {
            var header = new byte[2];
            while(running)
            {
                if(!ReadExactly(stream, header, 2))
                {
                    return;
                }
                var length = (header[0] << 8) | header[1];
                if(length < 2 || length > MaxNotificationLength + 1)
                {
                    throw new IOException(string.Format("ANCS frame of {0} bytes, the channel takes 2..{1}",
                                                        length, MaxNotificationLength + 1));
                }
                var frame = new byte[length];
                if(!ReadExactly(stream, frame, length))
                {
                    return;
                }
                var payload = new byte[length - 1];
                Array.Copy(frame, 1, payload, 0, payload.Length);
                switch(frame[0])
                {
                case AnnounceKind:
                    Post(BuildHvx(NotificationSourceValue, payload));
                    break;
                case AttributesKind:
                    Post(BuildHvx(DataSourceValue, payload));
                    break;
                default:
                    throw new IOException(string.Format("ANCS frame of kind 0x{0:x2}", frame[0]));
                }
            }
        }

        private void ToAncsClient(byte kind, byte[] payload)
        {
            var queue = ancsOutbound;
            if(queue == null)
            {
                this.Log(LogLevel.Warning, "ANCS message of kind 0x{0:x2} with no client connected, dropped", kind);
                return;
            }
            var message = new byte[payload.Length + 1];
            message[0] = kind;
            Array.Copy(payload, 0, message, 1, payload.Length);
            queue.Add(message);
        }

        // The four vendor UUIDs the ANCS module registered with the SoftDevice,
        // read back out of its own cells the way the WPP handles are: the type
        // byte is whatever sd_ble_uuid_vs_add handed out for the base, and the
        // module matches a discovered characteristic on the pair.
        private byte[] VendorUuid(ulong offset)
        {
            return machine.SystemBus.ReadBytes(ancsUuidTable + offset, 3, true);
        }

        private Func<byte[]> BuildPrimaryServiceDiscoveryResponse(bool found)
        {
            return () =>
            {
                var body = new byte[6 + (found ? 8 : 0)];
                Write16(body, 0, found ? GattStatusSuccess : GattStatusAttributeNotFound);
                Write16(body, 4, (ushort)(found ? 1 : 0));
                if(found)
                {
                    // ble_gattc_service_t: uuid(2), type(1), pad(1), start(2), end(2).
                    Array.Copy(VendorUuid(ServiceUuidOffset), 0, body, 6, 3);
                    Write16(body, 10, ServiceStart);
                    Write16(body, 12, ServiceEnd);
                }
                return Event(GattcEvtPrimarySrvcDiscRsp, body);
            };
        }

        private Func<byte[]> BuildCharacteristicDiscoveryResponse(ushort startHandle, ushort endHandle)
        {
            return () =>
            {
                var found = Characteristics().Where(c => c.Declaration >= startHandle && c.Declaration <= endHandle).ToArray();
                var body = new byte[6 + CharacteristicSize * found.Length];
                Write16(body, 0, found.Length > 0 ? GattStatusSuccess : GattStatusAttributeNotFound);
                Write16(body, 4, (ushort)found.Length);
                for(var index = 0; index < found.Length; index++)
                {
                    var at = 6 + CharacteristicSize * index;
                    Array.Copy(found[index].Uuid, 0, body, at, 3);
                    body[at + 3] = found[index].Properties;
                    Write16(body, at + 6, found[index].Declaration);
                    Write16(body, at + 8, found[index].Value);
                }
                return Event(GattcEvtCharDiscRsp, body);
            };
        }

        private Func<byte[]> BuildDescriptorDiscoveryResponse(ushort startHandle, ushort endHandle)
        {
            return () =>
            {
                var found = Characteristics()
                    .Where(c => c.Configuration != 0 && c.Configuration >= startHandle && c.Configuration <= endHandle)
                    .Select(c => c.Configuration).ToArray();
                var body = new byte[6 + DescriptorSize * found.Length];
                Write16(body, 0, found.Length > 0 ? GattStatusSuccess : GattStatusAttributeNotFound);
                Write16(body, 4, (ushort)found.Length);
                for(var index = 0; index < found.Length; index++)
                {
                    var at = 6 + DescriptorSize * index;
                    Write16(body, at, found[index]);
                    Write16(body, at + 2, ClientConfigurationUuid);
                    body[at + 4] = BleUuidType;
                }
                return Event(GattcEvtDescDiscRsp, body);
            };
        }

        private Func<byte[]> BuildWriteResponse(ushort handle, byte writeOperation, ushort offset, byte[] value)
        {
            return () =>
            {
                // ble_gattc_evt_write_rsp_t behind gatt_status and error_handle:
                // handle(2), write_op(1), pad(1), offset(2), len(2), data[].
                var body = new byte[12 + value.Length];
                Write16(body, 0, GattStatusSuccess);
                Write16(body, 4, handle);
                body[6] = writeOperation;
                Write16(body, 8, offset);
                Write16(body, 10, (ushort)value.Length);
                Array.Copy(value, 0, body, 12, value.Length);
                return Event(GattcEvtWriteRsp, body);
            };
        }

        private Func<byte[]> BuildHvx(ushort handle, byte[] value)
        {
            return () =>
            {
                var body = new byte[10 + value.Length];
                Write16(body, 0, GattStatusSuccess);
                Write16(body, 4, handle);
                body[6] = HvxNotification;
                Write16(body, 8, (ushort)value.Length);
                Array.Copy(value, 0, body, 10, value.Length);
                return Event(GattcEvtHvx, body);
            };
        }

        private AncsCharacteristic[] Characteristics()
        {
            return new[]
            {
                new AncsCharacteristic
                {
                    Uuid = VendorUuid(NotificationSourceUuidOffset), Properties = NotifyProperty,
                    Declaration = NotificationSourceDeclaration, Value = NotificationSourceValue,
                    Configuration = NotificationSourceConfiguration,
                },
                new AncsCharacteristic
                {
                    Uuid = VendorUuid(ControlPointUuidOffset), Properties = WriteProperty,
                    Declaration = ControlPointDeclaration, Value = ControlPointValue,
                    Configuration = 0,
                },
                new AncsCharacteristic
                {
                    Uuid = VendorUuid(DataSourceUuidOffset), Properties = NotifyProperty,
                    Declaration = DataSourceDeclaration, Value = DataSourceValue,
                    Configuration = DataSourceConfiguration,
                },
            };
        }

        private struct AncsCharacteristic
        {
            public byte[] Uuid;
            public byte Properties;
            public ushort Declaration;
            public ushort Value;
            public ushort Configuration;
        }

        private void Answer(ICpuSupportingGdb source, ulong address, Func<byte[]> build)
        {
            Post(build);
            source.SetRegisterUlong(ResultRegister, NrfSuccess);
            source.PC = address + 2;
        }

        // Every GATT client procedure on the link runs through one queue in the
        // firmware and the next one only starts when the last has ended, so the
        // pipe has to answer all of them, not only the ANCS ones: a discovery
        // left to the SoftDevice fails, its owner retries it on a timer, and
        // ANCS never gets a turn. Only the ANCS service is found, and a
        // discovery for anything else is answered the way a link with no such
        // service answers it.
        private void OnPrimaryServicesDiscover(ICpuSupportingGdb source, ulong address)
        {
            var startHandle = (ushort)source.GetRegisterUlong(SecondArgumentRegister);
            var parameters = source.GetRegisterUlong(ThirdArgumentRegister);
            var uuid = machine.SystemBus.ReadWord(parameters);
            var type = machine.SystemBus.ReadByte(parameters + 2);
            var wanted = VendorUuid(ServiceUuidOffset);
            var ancs = uuid == (ushort)(wanted[0] | (wanted[1] << 8)) && type == wanted[2];
            this.Log(ancs ? LogLevel.Info : LogLevel.Noisy,
                     "gattc discovery from 0x{0:x4} for uuid 0x{1:x4} type {2}{3}",
                     startHandle, uuid, type, ancs ? ", the ANCS service" : "");
            Answer(source, address, BuildPrimaryServiceDiscoveryResponse(ancs && startHandle <= ServiceStart));
        }

        private void OnCharacteristicsDiscover(ICpuSupportingGdb source, ulong address)
        {
            var range = source.GetRegisterUlong(SecondArgumentRegister);
            var startHandle = machine.SystemBus.ReadWord(range);
            var endHandle = machine.SystemBus.ReadWord(range + 2);
            this.Log(LogLevel.Info, "gattc characteristic discovery over [0x{0:x4}, 0x{1:x4}]", startHandle, endHandle);
            Answer(source, address, BuildCharacteristicDiscoveryResponse(startHandle, endHandle));
        }

        private void OnDescriptorsDiscover(ICpuSupportingGdb source, ulong address)
        {
            var range = source.GetRegisterUlong(SecondArgumentRegister);
            var startHandle = machine.SystemBus.ReadWord(range);
            var endHandle = machine.SystemBus.ReadWord(range + 2);
            this.Log(LogLevel.Info, "gattc descriptor discovery over [0x{0:x4}, 0x{1:x4}]", startHandle, endHandle);
            Answer(source, address, BuildDescriptorDiscoveryResponse(startHandle, endHandle));
        }

        // A value the firmware can fit in one ATT payload it writes as a write
        // request; a longer one it prepares in pieces and then executes, and
        // only the execute makes it a write. Each half gets its own response,
        // which is what the firmware's own chunking counts on. A write command
        // is unacknowledged, and answering one would call a callback that
        // belongs to whatever wrote last.
        private void OnGattcWrite(ICpuSupportingGdb source, ulong address)
        {
            var parameters = source.GetRegisterUlong(SecondArgumentRegister);
            // ble_gattc_write_params_t: write_op(1), flags(1), handle(2),
            // offset(2), len(2), p_value(4).
            var operation = machine.SystemBus.ReadByte(parameters);
            var handle = machine.SystemBus.ReadWord(parameters + 2);
            var offset = machine.SystemBus.ReadWord(parameters + 4);
            var length = machine.SystemBus.ReadWord(parameters + 6);
            var value = length == 0
                ? new byte[0]
                : machine.SystemBus.ReadBytes(machine.SystemBus.ReadDoubleWord(parameters + 8), length, true);
            switch(operation)
            {
            case PrepareWriteRequest:
                List<byte> buffer;
                if(!pending.TryGetValue(handle, out buffer))
                {
                    buffer = new List<byte>();
                    pending[handle] = buffer;
                }
                buffer.AddRange(value);
                break;
            case ExecuteWriteRequest:
                foreach(var entry in pending)
                {
                    Written(entry.Key, entry.Value.ToArray());
                }
                pending.Clear();
                break;
            case WriteCommand:
                Written(handle, value);
                return;
            default:
                Written(handle, value);
                break;
            }
            Answer(source, address, BuildWriteResponse(handle, operation, offset, value));
        }

        // A characteristic the watch wrote, whole. The two configuration
        // descriptors are the subscriptions, and the client is told once both
        // are on: until then a notification on either source goes nowhere, and
        // the firmware logs it as an hvx for an unregistered handle.
        private void Written(ushort handle, byte[] value)
        {
            if(handle == ControlPointValue)
            {
                this.Log(LogLevel.Info, "ANCS control point write of {0} bytes", value.Length);
                ToAncsClient(RequestKind, value);
                return;
            }
            var enabled = value.Length >= 2 && (value[0] | (value[1] << 8)) != 0;
            this.Log(LogLevel.Info, "ANCS subscription on handle 0x{0:x4} {1}", handle, enabled ? "on" : "off");
            if(handle != NotificationSourceConfiguration && handle != DataSourceConfiguration)
            {
                return;
            }
            subscribed[handle == NotificationSourceConfiguration ? 0 : 1] = enabled;
            if(subscribed[0] && subscribed[1])
            {
                ToAncsClient(SubscribedKind, new byte[0]);
            }
        }

        // Cortex-M register indices in Renode's ARM register enumeration.
        private const int ResultRegister = 0;
        private const int SecondArgumentRegister = 1;
        private const int ThirdArgumentRegister = 2;

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

        // The ANCS service as the pipe serves it, at handles it picks: nothing
        // on either side names an attribute by number, the watch discovers
        // every handle it uses, and a run is reproducible because the table is
        // a constant rather than an allocation.
        private const ushort ServiceStart = 0x0010;
        private const ushort NotificationSourceDeclaration = 0x0011;
        private const ushort NotificationSourceValue = 0x0012;
        private const ushort NotificationSourceConfiguration = 0x0013;
        private const ushort ControlPointDeclaration = 0x0014;
        private const ushort ControlPointValue = 0x0015;
        private const ushort DataSourceDeclaration = 0x0016;
        private const ushort DataSourceValue = 0x0017;
        private const ushort DataSourceConfiguration = 0x0018;
        private const ushort ServiceEnd = DataSourceConfiguration;
        private const ushort ClientConfigurationUuid = 0x2902;
        private const byte BleUuidType = 0x01;
        private const byte NotifyProperty = 0x10;
        private const byte WriteProperty = 0x08;
        // ble_gattc_service_t, ble_gattc_char_t and ble_gattc_desc_t as the
        // compiler laid them out, which is what the firmware's handlers step
        // by: the padding after the three byte UUID is part of the stride.
        private const int CharacteristicSize = 10;
        private const int DescriptorSize = 6;
        private const ushort GattcEvtPrimarySrvcDiscRsp = 0x30;
        private const ushort GattcEvtCharDiscRsp = 0x32;
        private const ushort GattcEvtDescDiscRsp = 0x33;
        private const ushort GattcEvtWriteRsp = 0x38;
        private const ushort GattcEvtHvx = 0x39;
        private const ushort GattStatusSuccess = 0x0000;
        private const ushort GattStatusAttributeNotFound = 0x010a;
        private const byte HvxNotification = 0x01;
        private const byte WriteCommand = 0x02;
        private const byte PrepareWriteRequest = 0x04;
        private const byte ExecuteWriteRequest = 0x05;
        private const byte AnnounceKind = 0x01;
        private const byte RequestKind = 0x02;
        private const byte AttributesKind = 0x03;
        private const byte SubscribedKind = 0x04;
        private const int MaxNotificationLength = EventBufferLength - 16;
        private const ulong BondedAddressOffset = 0x0a;
        // The four ble_uuid_t the ANCS module fills in at discovery time, in
        // the order it lays them out: data source, notification source,
        // control point, service.
        private const ulong DataSourceUuidOffset = 0x0;
        private const ulong NotificationSourceUuidOffset = 0x4;
        private const ulong ControlPointUuidOffset = 0x8;
        private const ulong ServiceUuidOffset = 0xc;

        private ICPUWithHooks cpu;
        private ulong present;
        private ulong poll;
        private ulong hvx0;
        private ulong discoverServices;
        private ulong discoverCharacteristics;
        private ulong discoverDescriptors;
        private ulong gattcWrite;
        private TcpListener listener;
        private TcpListener ancsListener;
        private Thread worker;
        private Thread ancsWorker;
        private volatile bool running = true;

        private readonly IMachine machine;
        private readonly int port;
        private readonly ulong evtBuffer;
        private readonly ulong wppServiceContext;
        private readonly int sdEventIrq;
        private readonly int ancsPort;
        private readonly ulong pairingRecord;
        private readonly ulong ancsUuidTable;
        private readonly ConcurrentQueue<byte[]> inbound;
        private readonly Dictionary<ushort, List<byte>> pending;
        private readonly bool[] subscribed = new bool[2];
        private volatile BlockingCollection<byte[]> outbound;
        private volatile BlockingCollection<byte[]> ancsOutbound;
    }
}
