package dev.davidv.withoutings.ble

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothProfile
import android.bluetooth.BluetoothStatusCodes
import android.content.Context
import android.util.Log
import java.util.UUID

/**
 * One GATT connection to a watch, from `connectGatt` to a live protocol
 * channel, and the byte stream in both directions over it.
 *
 * Everything above this — scanning, retries, what the frames mean — belongs to
 * the caller. What is here is the part that is the same whether the watch is
 * being paired with or synced from, and the part that has to be got right
 * exactly once: a write larger than the MTU is not split by the stack, and a
 * second write issued before the first completes is silently dropped.
 */
class GattLink(
    private val context: Context,
    private val listener: Listener,
) {
    interface Listener {
        /** Connected, but nothing can be sent until [onReady]. */
        fun onConnected()

        /** The protocol channel is live and its notifications are subscribed. */
        fun onReady()

        /** One notification's worth of the byte stream. Frames span these. */
        fun onBytes(bytes: ByteArray)

        /** The link is gone, whether or not it was ever ready. */
        fun onDisconnected(status: Int)

        /** No characteristic on the device carries the protocol. */
        fun onChannelMissing()
    }

    private var gatt: BluetoothGatt? = null
    private var channel: BluetoothGattCharacteristic? = null

    /// What the link actually granted, which is not necessarily [MTU].
    private var negotiatedMtu = DEFAULT_MTU

    private val pending = ArrayDeque<ByteArray>()
    private var writeInFlight = false
    private var writeStuckSince = 0L
    private var framesOut = 0
    private var framesIn = 0
    private var connectedAt = 0L

    @SuppressLint("MissingPermission")
    fun connect(device: BluetoothDevice) {
        close()
        connectedAt = System.currentTimeMillis()
        gatt = device.connectGatt(context, false, callback, BluetoothDevice.TRANSPORT_LE)
    }

    /**
     * One frame, cut to fit the link.
     *
     * A GATT write carries at most the MTU less the ATT header, and the watch
     * reassembles the byte stream on its side exactly as we do on ours.
     * Handing the stack an oversized write does not split it — it fails, or
     * worse arrives truncated and is parsed as a corrupt frame. Everything
     * sent before images was small enough for this never to come up.
     */
    fun write(bytes: ByteArray) {
        val limit = (negotiatedMtu - ATT_HEADER).coerceAtLeast(1)
        if (WIRE_LOG) Log.i(WIRE_TAG, "-> ${bytes.hex()}")
        // Every frame, not only the split ones: a set is a run of frames and
        // the small ones — a bare terminator, a short entry — are exactly the
        // ones worth seeing when a run of them does not take.
        Log.i(TAG, "frame of ${bytes.size} in ${(bytes.size + limit - 1) / limit} writes")
        synchronized(pending) {
            var offset = 0
            while (offset < bytes.size) {
                val end = minOf(offset + limit, bytes.size)
                pending.addLast(bytes.copyOfRange(offset, end))
                offset = end
            }
        }
        drain()
    }

    /**
     * Let go of the connection and everything queued on it.
     *
     * A write only completes on the connection that issued it, so keeping the
     * in-flight flag across a teardown wedges every later connection: the
     * callback that would clear it cannot arrive, so nothing is ever written
     * again and the watch has nothing to answer. The queued frames go too —
     * they belong to a session that no longer exists.
     */
    @SuppressLint("MissingPermission")
    fun close() {
        gatt?.close()
        gatt = null
        channel = null
        synchronized(pending) {
            pending.clear()
            writeInFlight = false
            writeStuckSince = 0L
        }
        negotiatedMtu = DEFAULT_MTU
    }

    /// Everything needed to tell a wedge from a quiet watch, in one line.
    fun describe(): String {
        val queued = synchronized(pending) { pending.size }
        val stuck = if (writeStuckSince == 0L) 0 else
            (System.currentTimeMillis() - writeStuckSince) / 1000
        val alive = if (connectedAt == 0L) 0 else
            (System.currentTimeMillis() - connectedAt) / 1000
        return "[out=$framesOut in=$framesIn queued=$queued inFlight=${writeInFlight}" +
            (if (stuck > 0) " stuckFor=${stuck}s" else "") + " linkAge=${alive}s]"
    }

    private val callback = object : BluetoothGattCallback() {
        @SuppressLint("MissingPermission")
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            Log.i(TAG, "connectionStateChange status=$status newState=$newState")
            when (newState) {
                BluetoothProfile.STATE_CONNECTED -> {
                    listener.onConnected()
                    g.requestMtu(MTU)
                }

                BluetoothProfile.STATE_DISCONNECTED -> {
                    channel = null
                    listener.onDisconnected(status)
                }
            }
        }

        @SuppressLint("MissingPermission")
        override fun onMtuChanged(g: BluetoothGatt, mtu: Int, status: Int) {
            Log.i(TAG, "mtu=$mtu status=$status")
            // What we asked for is not necessarily what we got, and every
            // outbound frame is cut to fit it.
            negotiatedMtu = mtu
            g.discoverServices()
        }

        @SuppressLint("MissingPermission")
        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            Log.i(TAG, "discovered ${g.services.size} services, status=$status")
            val characteristic = findProtocolChannel(g)
            if (characteristic == null) {
                g.services.forEach { svc ->
                    Log.w(TAG, "service ${svc.uuid}")
                    svc.characteristics.forEach { Log.w(TAG, "  characteristic ${it.uuid}") }
                }
                Log.e(TAG, "protocol characteristic not found")
                listener.onChannelMissing()
                return
            }
            Log.i(TAG, "protocol channel ${characteristic.uuid}")
            channel = characteristic
            g.setCharacteristicNotification(characteristic, true)
            characteristic.getDescriptor(CLIENT_CONFIG)?.let {
                g.writeDescriptor(it, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE)
            }
        }

        override fun onDescriptorWrite(g: BluetoothGatt, d: BluetoothGattDescriptor, status: Int) {
            Log.i(TAG, "notifications enabled, status=$status")
            // Only now will the watch's replies actually reach us.
            listener.onReady()
        }

        override fun onCharacteristicChanged(
            g: BluetoothGatt,
            c: BluetoothGattCharacteristic,
            value: ByteArray,
        ) {
            framesIn++
            if (WIRE_LOG) Log.i(WIRE_TAG, "<- ${value.hex()}")
            listener.onBytes(value)
        }

        override fun onCharacteristicWrite(
            g: BluetoothGatt,
            c: BluetoothGattCharacteristic,
            status: Int,
        ) {
            // A callback from a connection already replaced would clear the
            // flag belonging to the current one, letting two writes overlap.
            if (g !== gatt) return
            if (status != BluetoothGatt.GATT_SUCCESS) Log.w(TAG, "write failed status=$status")
            synchronized(pending) {
                writeInFlight = false
                writeStuckSince = 0L
            }
            drain()
        }
    }

    /**
     * One outstanding write at a time: a second write before the first
     * completes is silently dropped by the stack.
     */
    @SuppressLint("MissingPermission")
    private fun drain() {
        val g = gatt ?: return
        val c = channel ?: return
        val next = synchronized(pending) {
            if (writeInFlight || pending.isEmpty()) return@synchronized null
            writeInFlight = true
            writeStuckSince = System.currentTimeMillis()
            pending.removeFirst()
        } ?: return
        // A write that never reaches the stack never calls back, so nothing
        // else would clear the flag and every later frame would queue behind it
        // forever. It can fail either way: a status when the stack is up and
        // says no, an exception when the stack has gone away underneath.
        val status = runCatching {
            g.writeCharacteristic(c, next, BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT)
        }.getOrElse { failure ->
            Log.w(TAG, "write threw", failure)
            BluetoothStatusCodes.ERROR_UNKNOWN
        }
        if (status != BluetoothStatusCodes.SUCCESS) {
            Log.w(TAG, "write refused status=$status")
            synchronized(pending) { writeInFlight = false }
        } else {
            framesOut++
        }
    }

    /**
     * The protocol rides on characteristic "handle 4" of the Withings service;
     * both UUIDs carry ASCII markers ("WITH", "INGS") rather than being
     * assigned numbers, so they are matched rather than compared.
     */
    private fun findProtocolChannel(g: BluetoothGatt): BluetoothGattCharacteristic? =
        g.services
            .filter { it.uuid.toString().contains(WITHINGS_SERVICE_MARKER) }
            .flatMap { it.characteristics }
            .firstOrNull { c ->
                c.uuid.toString().uppercase().contains(WITHINGS_CHARACTERISTIC_MARKER) &&
                    c.uuid.toString().getOrNull(7) == PROTOCOL_HANDLE
            }

    companion object {
        private const val TAG = "GattLink"

        /// Every WPP write and notification as hex, for diffing against a
        /// capture of the official app. Noisy: one line per frame.
        private const val WIRE_LOG = false
        private const val WIRE_TAG = "Wpp"

        private const val MTU = 512

        /// Before the link says otherwise, the BLE default.
        private const val DEFAULT_MTU = 23

        /// Opcode and handle, ahead of every write payload.
        private const val ATT_HEADER = 3

        private const val WITHINGS_SERVICE_MARKER = "5749-5448"
        private const val WITHINGS_CHARACTERISTIC_MARKER = "494E4753"
        private const val PROTOCOL_HANDLE = '4'
        private val CLIENT_CONFIG: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

        /**
         * A watch is anything advertising the name the family uses. The
         * address cannot be matched on — it is random and rotates — and the
         * advertisement carries no service UUID to filter by either.
         */
        const val DEVICE_NAME_PREFIX = "ScanWatch"

        private fun ByteArray.hex(): String = joinToString(" ") { "%02x".format(it) }
    }
}
