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
import android.os.Handler
import android.os.Looper
import android.util.Log
import java.util.UUID

/**
 * A write larger than the MTU is not split by the stack, and a second write
 * issued before the first completes is silently dropped.
 */
class GattLink(
    private val context: Context,
    private val listener: Listener,
) {
    interface Listener {
        fun onConnected()

        fun onReady()

        fun onBytes(bytes: ByteArray)

        fun onDisconnected(status: Int)

        fun onChannelMissing()
    }

    private class Channel(
        val data: BluetoothGattCharacteristic,
        val config: BluetoothGattDescriptor,
    )

    // Android runs one GATT operation per connection at a time and refuses
    // another while one is in flight, so the CCCD write and the frame chunks
    // take turns in a single slot.
    private enum class Op { Subscribe, Write }

    // GATT callbacks arrive on a binder thread, retries on the main looper and
    // writes from the caller's thread; every field below is guarded by it.
    private val lock = Any()

    private var gatt: BluetoothGatt? = null
    private var channel: Channel? = null
    private var notifying = false
    private var ready = false

    private var negotiatedMtu = DEFAULT_MTU

    private val pending = ArrayDeque<ByteArray>()
    private var inFlight: Op? = null
    private var subscribeDue = false
    private var subscribeAttempts = 0
    private var writeStuckSince = 0L
    private var framesOut = 0
    private var framesIn = 0
    private var connectedAt = 0L

    private val handler = Handler(Looper.getMainLooper())
    private val resubscribe = Runnable {
        synchronized(lock) {
            subscribeDue = true
            startNext()
        }
    }

    @SuppressLint("MissingPermission")
    fun connect(device: BluetoothDevice) {
        close()
        synchronized(lock) {
            connectedAt = System.currentTimeMillis()
            gatt = device.connectGatt(context, false, callback, BluetoothDevice.TRANSPORT_LE)
        }
    }

    fun write(frames: List<ByteArray>) {
        synchronized(lock) {
            val limit = (negotiatedMtu - ATT_HEADER).coerceAtLeast(1)
            for (bytes in frames) {
                if (WIRE_LOG) Log.i(WIRE_TAG, "-> ${bytes.hex()}")
                Log.i(TAG, "frame of ${bytes.size} in ${(bytes.size + limit - 1) / limit} writes")
                var offset = 0
                while (offset < bytes.size) {
                    val end = minOf(offset + limit, bytes.size)
                    pending.addLast(bytes.copyOfRange(offset, end))
                    offset = end
                }
            }
            startNext()
        }
    }

    @SuppressLint("MissingPermission")
    fun close() {
        synchronized(lock) {
            handler.removeCallbacks(resubscribe)
            gatt?.close()
            gatt = null
            channel = null
            notifying = false
            ready = false
            pending.clear()
            inFlight = null
            subscribeDue = false
            subscribeAttempts = 0
            writeStuckSince = 0L
            negotiatedMtu = DEFAULT_MTU
        }
    }

    fun describe(): String = synchronized(lock) {
        val stuck = if (writeStuckSince == 0L) 0 else
            (System.currentTimeMillis() - writeStuckSince) / 1000
        val alive = if (connectedAt == 0L) 0 else
            (System.currentTimeMillis() - connectedAt) / 1000
        "[out=$framesOut in=$framesIn queued=${pending.size} inFlight=${inFlight ?: "none"}" +
            (if (stuck > 0) " stuckFor=${stuck}s" else "") + " linkAge=${alive}s]"
    }

    private fun isCurrent(g: BluetoothGatt): Boolean = synchronized(lock) { g === gatt }

    private val callback = object : BluetoothGattCallback() {
        @SuppressLint("MissingPermission")
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            if (!isCurrent(g)) return
            Log.i(TAG, "connectionStateChange status=$status newState=$newState")
            when (newState) {
                BluetoothProfile.STATE_CONNECTED -> {
                    listener.onConnected()
                    g.requestMtu(MTU)
                }

                BluetoothProfile.STATE_DISCONNECTED -> {
                    synchronized(lock) {
                        channel = null
                        notifying = false
                    }
                    listener.onDisconnected(status)
                }
            }
        }

        @SuppressLint("MissingPermission")
        override fun onMtuChanged(g: BluetoothGatt, mtu: Int, status: Int) {
            synchronized(lock) {
                if (g !== gatt) return
                Log.i(TAG, "mtu=$mtu status=$status")
                negotiatedMtu = mtu
                discover(g)
            }
        }

        @SuppressLint("MissingPermission")
        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            synchronized(lock) {
                if (g !== gatt) return
                Log.i(TAG, "discovered ${g.services.size} services, status=$status")
                if (status != BluetoothGatt.GATT_SUCCESS) {
                    Log.e(TAG, "service discovery failed, dropping the link")
                    g.disconnect()
                    return
                }
                val found = findProtocolChannel(g)
                if (found != null) {
                    Log.i(TAG, "protocol channel ${found.data.uuid}")
                    channel = found
                    notifying = false
                    subscribeAttempts = 0
                    handler.removeCallbacks(resubscribe)
                    subscribeDue = true
                    startNext()
                    return
                }
                g.services.forEach { svc ->
                    Log.w(TAG, "service ${svc.uuid}")
                    svc.characteristics.forEach { Log.w(TAG, "  characteristic ${it.uuid}") }
                }
            }
            Log.e(TAG, "protocol characteristic with a CCCD not found")
            listener.onChannelMissing()
        }

        // The ScanWatch 1 sends a Service Changed indication right after
        // connecting. Handles may have moved, so nothing is written until the
        // channel is found and subscribed again.
        @SuppressLint("MissingPermission")
        override fun onServiceChanged(g: BluetoothGatt) {
            synchronized(lock) {
                if (g !== gatt) return
                Log.i(TAG, "service changed, rediscovering")
                channel = null
                notifying = false
                subscribeDue = false
                handler.removeCallbacks(resubscribe)
                discover(g)
            }
        }

        override fun onDescriptorWrite(g: BluetoothGatt, d: BluetoothGattDescriptor, status: Int) {
            val firstReady = synchronized(lock) {
                if (g !== gatt) return
                Log.i(TAG, "notifications enabled, status=$status attempt=$subscribeAttempts")
                inFlight = null
                when (status) {
                    BluetoothGatt.GATT_SUCCESS -> {
                        notifying = true
                        val first = !ready
                        ready = true
                        startNext()
                        first
                    }

                    // The ScanWatch 1 refuses the CCCD write until link
                    // encryption with the bonded keys has completed.
                    BluetoothGatt.GATT_INSUFFICIENT_AUTHENTICATION,
                    BluetoothGatt.GATT_INSUFFICIENT_ENCRYPTION -> {
                        retrySubscribe(g, "status=$status")
                        startNext()
                        false
                    }

                    else -> {
                        Log.e(TAG, "could not enable notifications, status=$status, dropping the link")
                        g.disconnect()
                        false
                    }
                }
            }
            if (firstReady) listener.onReady()
        }

        override fun onCharacteristicChanged(
            g: BluetoothGatt,
            c: BluetoothGattCharacteristic,
            value: ByteArray,
        ) {
            if (!isCurrent(g)) return
            framesIn++
            if (WIRE_LOG) Log.i(WIRE_TAG, "<- ${value.hex()}")
            listener.onBytes(value)
        }

        override fun onCharacteristicWrite(
            g: BluetoothGatt,
            c: BluetoothGattCharacteristic,
            status: Int,
        ) {
            synchronized(lock) {
                if (g !== gatt) return
                if (status != BluetoothGatt.GATT_SUCCESS) Log.w(TAG, "write failed status=$status")
                inFlight = null
                writeStuckSince = 0L
                startNext()
            }
        }
    }

    @SuppressLint("MissingPermission")
    private fun discover(g: BluetoothGatt) {
        if (g.discoverServices()) return
        Log.e(TAG, "service discovery refused, dropping the link")
        g.disconnect()
    }

    // Caller holds the lock.
    @SuppressLint("MissingPermission")
    private fun startNext() {
        val g = gatt ?: return
        val c = channel ?: return
        if (inFlight != null) return
        if (subscribeDue) {
            subscribeDue = false
            subscribe(g, c)
            return
        }
        if (!notifying) return
        val next = pending.firstOrNull() ?: return
        // A write that never reaches the stack never calls back, so nothing
        // else would clear the flag and every later frame would queue behind
        // it forever.
        val status = runCatching {
            g.writeCharacteristic(c.data, next, BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT)
        }.getOrElse { failure ->
            Log.w(TAG, "write threw", failure)
            BluetoothStatusCodes.ERROR_UNKNOWN
        }
        if (status != BluetoothStatusCodes.SUCCESS) {
            Log.w(TAG, "write refused status=$status")
            return
        }
        pending.removeFirst()
        inFlight = Op.Write
        writeStuckSince = System.currentTimeMillis()
        framesOut++
    }

    // Caller holds the lock.
    @SuppressLint("MissingPermission")
    private fun subscribe(g: BluetoothGatt, c: Channel) {
        subscribeAttempts++
        g.setCharacteristicNotification(c.data, true)
        val status = g.writeDescriptor(c.config, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE)
        Log.i(TAG, "CCCD write attempt $subscribeAttempts queued status=$status")
        when (status) {
            BluetoothStatusCodes.SUCCESS -> inFlight = Op.Subscribe
            BluetoothStatusCodes.ERROR_GATT_WRITE_REQUEST_BUSY -> retrySubscribe(g, "stack busy")
            else -> {
                Log.e(TAG, "CCCD write refused, status=$status, dropping the link")
                g.disconnect()
            }
        }
    }

    // Caller holds the lock.
    @SuppressLint("MissingPermission")
    private fun retrySubscribe(g: BluetoothGatt, reason: String) {
        if (subscribeAttempts >= SUBSCRIBE_MAX_ATTEMPTS) {
            Log.e(TAG, "could not enable notifications ($reason), dropping the link")
            g.disconnect()
            return
        }
        Log.w(TAG, "CCCD write attempt $subscribeAttempts failed ($reason), retrying")
        handler.postDelayed(resubscribe, SUBSCRIBE_RETRY_MS)
    }

    private fun findProtocolChannel(g: BluetoothGatt): Channel? =
        g.services
            .filter { it.uuid.toString().contains(WITHINGS_SERVICE_MARKER) }
            .flatMap { it.characteristics }
            .filter { c ->
                val id = c.uuid.toString().uppercase()
                (id.contains(WITHINGS_CHARACTERISTIC_MARKER) ||
                    id.contains(LEGACY_CHARACTERISTIC_MARKER)) &&
                    id.getOrNull(7) == PROTOCOL_HANDLE
            }
            .firstNotNullOfOrNull { c -> c.getDescriptor(CLIENT_CONFIG)?.let { Channel(c, it) } }

    companion object {
        private const val TAG = "GattLink"

        private const val WIRE_LOG = false
        private const val WIRE_TAG = "Wpp"

        private const val MTU = 512

        private const val DEFAULT_MTU = 23

        private const val ATT_HEADER = 3

        private const val WITHINGS_SERVICE_MARKER = "5749-5448"
        private const val WITHINGS_CHARACTERISTIC_MARKER = "494E4753"
        // Original ScanWatch (1) exposes the protocol channel as
        // 00000024-5749-5448-005d-000000000000 (write + notify).
        private const val LEGACY_CHARACTERISTIC_MARKER = "5749-5448-005D"
        private const val PROTOCOL_HANDLE = '4'
        private const val SUBSCRIBE_RETRY_MS = 1000L
        private const val SUBSCRIBE_MAX_ATTEMPTS = 5
        private val CLIENT_CONFIG: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

        const val DEVICE_NAME_PREFIX = "ScanWatch"

        private fun ByteArray.hex(): String = joinToString(" ") { "%02x".format(it) }
    }
}
