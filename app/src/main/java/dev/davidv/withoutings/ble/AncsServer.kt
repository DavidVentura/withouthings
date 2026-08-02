package dev.davidv.withoutings.ble

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothGattServer
import android.bluetooth.BluetoothGattServerCallback
import android.bluetooth.BluetoothGattService
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.content.Context
import android.util.Log
import java.util.UUID
import uniffi.wpp_ffi.AncsLink
import uniffi.wpp_ffi.WatchService
import uniffi.wpp_ffi.ancsUuids

/**
 * The phone's half of phone notifications.
 *
 * Everywhere else in this app the phone is the GATT client and the watch the
 * peripheral. Here it is the other way round: the watch connects back over the
 * link that is already up and reads notifications out of a server we run, so
 * nothing is advertised and there is no second connection to manage.
 *
 * Nothing here understands the format. Writes go straight to [WatchService],
 * which hands back the bytes to notify with.
 */
class AncsServer(
    private val context: Context,
    private val service: () -> WatchService?,
) : AncsLink {

    // Taken from Rust rather than repeated, so the two sides cannot drift.
    private val uuids = ancsUuids()
    private val serviceUuid = UUID.fromString(uuids.service)
    private val notificationSource = UUID.fromString(uuids.notificationSource)
    private val controlPoint = UUID.fromString(uuids.controlPoint)
    private val dataSource = UUID.fromString(uuids.dataSource)

    private var server: BluetoothGattServer? = null
    private var client: BluetoothDevice? = null
    /** Until the watch negotiates otherwise, the BLE default. */
    private var mtu = DEFAULT_MTU
    /** Characteristics the watch has subscribed to, by CCCD write. */
    private val subscribed = HashSet<UUID>()

    /**
     * Notifications the stack has not taken yet.
     *
     * A second notify before the first is confirmed is dropped silently, and
     * a long message is several notifies, so they go out one at a time.
     */
    private val pending = ArrayDeque<Pair<UUID, ByteArray>>()
    private var inFlight = false

    @SuppressLint("MissingPermission")
    fun start() {
        if (server != null) return
        val manager = context.getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager
        val opened = runCatching { manager.openGattServer(context, callback) }.getOrNull()
        if (opened == null) {
            Log.e(TAG, "could not open a GATT server")
            return
        }
        server = opened
        opened.addService(buildService())
        Log.i(TAG, "ANCS server listening on $serviceUuid")
    }

    @SuppressLint("MissingPermission")
    fun stop() {
        discard()
        client = null
        mtu = DEFAULT_MTU
        subscribed.clear()
        runCatching { server?.close() }
        server = null
    }

    private fun buildService(): BluetoothGattService {
        val source = BluetoothGattCharacteristic(
            notificationSource,
            BluetoothGattCharacteristic.PROPERTY_NOTIFY,
            BluetoothGattCharacteristic.PERMISSION_READ,
        ).apply { addDescriptor(clientConfig()) }

        val control = BluetoothGattCharacteristic(
            controlPoint,
            BluetoothGattCharacteristic.PROPERTY_WRITE,
            BluetoothGattCharacteristic.PERMISSION_WRITE,
        )

        val data = BluetoothGattCharacteristic(
            dataSource,
            BluetoothGattCharacteristic.PROPERTY_NOTIFY,
            BluetoothGattCharacteristic.PERMISSION_READ,
        ).apply { addDescriptor(clientConfig()) }

        return BluetoothGattService(serviceUuid, BluetoothGattService.SERVICE_TYPE_PRIMARY).apply {
            addCharacteristic(source)
            addCharacteristic(control)
            addCharacteristic(data)
        }
    }

    private fun clientConfig() = BluetoothGattDescriptor(
        CLIENT_CONFIG,
        BluetoothGattDescriptor.PERMISSION_READ or BluetoothGattDescriptor.PERMISSION_WRITE,
    )

    override fun announce(bytes: ByteArray) = enqueue(notificationSource, bytes)

    override fun attributes(bytes: ByteArray) = enqueue(dataSource, bytes)

    /**
     * Queue a notification, if the watch has asked for them.
     *
     * The official app checks this before every announcement and drops the
     * ones it cannot deliver, rather than holding them for a subscriber that
     * may never arrive. Notifying a characteristic nobody subscribed to sends
     * the packet anyway — Android's server role does not check the CCCD for
     * you — so without this the watch receives traffic it never asked for.
     */
    private fun enqueue(characteristic: UUID, bytes: ByteArray) {
        if (characteristic !in subscribed) {
            Log.w(TAG, "dropped ${bytes.size} bytes: the watch is not subscribed to $characteristic")
            return
        }
        synchronized(pending) { pending.addLast(characteristic to bytes) }
        drain()
    }

    private fun discard() {
        synchronized(pending) {
            pending.clear()
            inFlight = false
        }
    }

    @SuppressLint("MissingPermission")
    private fun drain() {
        val target = client ?: return
        val gattServer = server ?: return
        val next = synchronized(pending) {
            if (inFlight || pending.isEmpty()) return@synchronized null
            inFlight = true
            pending.removeFirst()
        } ?: return

        val characteristic = gattServer.getService(serviceUuid)?.getCharacteristic(next.first)
        if (characteristic == null) {
            Log.e(TAG, "characteristic ${next.first} is missing from our own service")
            synchronized(pending) { inFlight = false }
            return
        }
        Log.i(
            TAG,
            "${if (next.first == notificationSource) "announce" else "attributes"} " +
                "-> ${next.second.hex()}",
        )
        // A notify that never reaches the stack never calls back, so nothing
        // would clear the flag and every later fragment would queue behind it.
        val status = runCatching {
            gattServer.notifyCharacteristicChanged(target, characteristic, false, next.second)
        }.getOrElse { failure ->
            Log.w(TAG, "notify threw", failure)
            BluetoothGatt.GATT_FAILURE
        }
        if (status != BluetoothGatt.GATT_SUCCESS) {
            Log.w(TAG, "notify to ${next.first} refused status=$status")
            synchronized(pending) { inFlight = false }
        }
    }

    private val callback = object : BluetoothGattServerCallback() {
        @SuppressLint("MissingPermission")
        override fun onConnectionStateChange(device: BluetoothDevice, status: Int, newState: Int) {
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                Log.i(TAG, "${device.address} connected to the ANCS server")
                client = device
                drain()
                return
            }
            Log.i(TAG, "${device.address} left the ANCS server")
            if (device != client) return
            client = null
            subscribed.clear()
            // The queue belongs to a session that no longer exists; keeping it
            // would deliver one notification's fragments into the next.
            discard()
        }

        override fun onMtuChanged(device: BluetoothDevice, size: Int) {
            Log.i(TAG, "ANCS mtu=$size")
            mtu = size
        }

        override fun onNotificationSent(device: BluetoothDevice, status: Int) {
            synchronized(pending) { inFlight = false }
            drain()
        }

        @SuppressLint("MissingPermission")
        override fun onCharacteristicWriteRequest(
            device: BluetoothDevice,
            requestId: Int,
            characteristic: BluetoothGattCharacteristic,
            preparedWrite: Boolean,
            responseNeeded: Boolean,
            offset: Int,
            value: ByteArray,
        ) {
            if (responseNeeded) {
                server?.sendResponse(device, requestId, BluetoothGatt.GATT_SUCCESS, offset, null)
            }
            if (characteristic.uuid != controlPoint) {
                Log.w(TAG, "write to ${characteristic.uuid}, which takes none")
                return
            }
            // The watch reads the answer off the Data Source, so a fragment can
            // only be as big as one notification on this link.
            val payload = (mtu - ATT_HEADER).coerceAtLeast(1)
            Log.i(TAG, "control point <- ${value.hex()}")
            runCatching { service()?.onAncsWrite(value, payload.toUInt()) }
                .onFailure { Log.w(TAG, "control point write", it) }
        }

        @SuppressLint("MissingPermission")
        override fun onDescriptorWriteRequest(
            device: BluetoothDevice,
            requestId: Int,
            descriptor: BluetoothGattDescriptor,
            preparedWrite: Boolean,
            responseNeeded: Boolean,
            offset: Int,
            value: ByteArray,
        ) {
            val characteristic = descriptor.characteristic.uuid
            val on = value.contentEquals(BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE)
            if (on) subscribed.add(characteristic) else subscribed.remove(characteristic)
            Log.i(TAG, "$characteristic notifications ${if (on) "on" else "off"}")
            if (responseNeeded) {
                server?.sendResponse(device, requestId, BluetoothGatt.GATT_SUCCESS, offset, null)
            }
        }

        @SuppressLint("MissingPermission")
        override fun onDescriptorReadRequest(
            device: BluetoothDevice,
            requestId: Int,
            offset: Int,
            descriptor: BluetoothGattDescriptor,
        ) {
            val on = descriptor.characteristic.uuid in subscribed
            server?.sendResponse(
                device, requestId, BluetoothGatt.GATT_SUCCESS, offset,
                if (on) BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
                else BluetoothGattDescriptor.DISABLE_NOTIFICATION_VALUE,
            )
        }
    }

    private companion object {
        const val TAG = "Ancs"

        fun ByteArray.hex(): String = joinToString(" ") { "%02x".format(it) }
        const val DEFAULT_MTU = 23
        /** Opcode and handle, ahead of every notification payload. */
        const val ATT_HEADER = 3
        val CLIENT_CONFIG: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
    }
}
