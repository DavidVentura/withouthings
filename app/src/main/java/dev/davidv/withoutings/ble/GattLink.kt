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

    private var gatt: BluetoothGatt? = null
    private var channel: BluetoothGattCharacteristic? = null

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

    fun write(frames: List<ByteArray>) {
        val limit = (negotiatedMtu - ATT_HEADER).coerceAtLeast(1)
        synchronized(pending) {
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
        }
        drain()
    }

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
            if (g !== gatt) return
            if (status != BluetoothGatt.GATT_SUCCESS) Log.w(TAG, "write failed status=$status")
            synchronized(pending) {
                writeInFlight = false
                writeStuckSince = 0L
            }
            drain()
        }
    }

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
        // else would clear the flag and every later frame would queue behind
        // it forever.
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

        private const val WIRE_LOG = false
        private const val WIRE_TAG = "Wpp"

        private const val MTU = 512

        private const val DEFAULT_MTU = 23

        private const val ATT_HEADER = 3

        private const val WITHINGS_SERVICE_MARKER = "5749-5448"
        private const val WITHINGS_CHARACTERISTIC_MARKER = "494E4753"
        private const val PROTOCOL_HANDLE = '4'
        private val CLIENT_CONFIG: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

        const val DEVICE_NAME_PREFIX = "ScanWatch"

        private fun ByteArray.hex(): String = joinToString(" ") { "%02x".format(it) }
    }
}
