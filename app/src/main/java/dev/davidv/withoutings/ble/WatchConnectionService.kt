package dev.davidv.withoutings.ble

import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothProfile
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.content.Intent
import android.os.IBinder
import android.util.Log
import dev.davidv.withoutings.LinkState
import dev.davidv.withoutings.Settings
import dev.davidv.withoutings.WatchRepository
import java.util.UUID
import uniffi.wpp_ffi.Progress
import uniffi.wpp_ffi.Transport
import uniffi.wpp_ffi.WatchService

/**
 * Holds the GATT link and hands every notification straight to Rust.
 *
 * Nothing here understands the protocol; it moves bytes and keeps the process
 * alive. Framing, decoding, storage and the delete-after-commit rule all live
 * on the other side of [WatchService].
 */
class WatchConnectionService : Service() {

    private var gatt: BluetoothGatt? = null
    private var channel: BluetoothGattCharacteristic? = null
    private var service: WatchService? = null
    private val pending = ArrayDeque<ByteArray>()
    private var writeInFlight = false
    private val handler = android.os.Handler(android.os.Looper.getMainLooper())
    private var scanning = false
    private var lastProgress: Progress? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        startForeground(NOTIFICATION_ID, notification("Connecting"))
    }

    @SuppressLint("MissingPermission")
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val settings = Settings(this)
        val mac = settings.mac
        val secret = settings.secret
        if (mac.isNullOrBlank() || secret.isNullOrBlank()) {
            WatchRepository.setLink(LinkState.Disconnected)
            Log.e(TAG, "no watch configured")
            stopSelf()
            return START_NOT_STICKY
        }

        if (service == null) {
            service = WatchService(
                dbPath = getDatabasePath("watch.db").also { it.parentFile?.mkdirs() }.absolutePath,
                mac = mac,
                secret = secret,
                transport = GattTransport(),
            ).also { WatchRepository.attach(it) }
        }

        if (gatt == null) {
            val adapter = (getSystemService(Context.BLUETOOTH_SERVICE) as android.bluetooth.BluetoothManager).adapter
            connect(adapter, mac)
        }
        // The link is the point of this service; restart it if we are killed.
        return START_STICKY
    }

    /**
     * Find the watch by its advertised name, not its address.
     *
     * The address in the protocol (`ProbeChallenge.mac`) identifies the watch
     * for the authentication hash; over the air it advertises under a random
     * address that changes, so no address filter can match it. The name is the
     * only stable handle, and the connection is made to whatever address the
     * advertisement carried.
     */
    @SuppressLint("MissingPermission")
    private fun connect(adapter: BluetoothAdapter, mac: String) {
        if (!adapter.isEnabled) {
            WatchRepository.setLink(LinkState.Disconnected)
            Log.e(TAG, "bluetooth is off")
            return
        }
        val scanner = adapter.bluetoothLeScanner
        if (scanner == null) {
            Log.e(TAG, "no BLE scanner")
            return
        }
        Log.i(TAG, "scanning for a device named $DEVICE_NAME_PREFIX (identity $mac)")
        WatchRepository.setLink(LinkState.Connecting)

        scanning = true
        scanner.startScan(
            emptyList(),
            ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build(),
            scanCallback,
        )
        heartbeat()
    }

    /**
     * A finished sync is only current as of when it finished, and the watch
     * only volunteers a sync request sometimes, so walk again on a timer.
     */
    private fun scheduleResync() {
        handler.removeCallbacks(resync)
        handler.postDelayed(resync, RESYNC_MS)
    }

    /**
     * The handshake is two round trips and has no timeout of its own, so a
     * dropped frame leaves the link up and the protocol waiting forever. Give
     * it a deadline and start over from the scan.
     */
    private fun watchHandshake() {
        handler.postDelayed({
            val progress = runCatching { service?.snapshot()?.progress }.getOrNull()
            if (progress == Progress.CONNECTING) {
                retryLater("handshake did not complete within ${HANDSHAKE_MS / 1000}s")
            }
        }, HANDSHAKE_MS)
    }

    private val resync = object : Runnable {
        override fun run() {
            runCatching { service?.syncNow() }
                .onFailure { Log.e(TAG, "resync", it) }
            handler.postDelayed(this, RESYNC_MS)
        }
    }

    @SuppressLint("MissingPermission")
    private fun stopScan() {
        if (!scanning) return
        scanning = false
        runCatching {
            (getSystemService(Context.BLUETOOTH_SERVICE) as android.bluetooth.BluetoothManager)
                .adapter.bluetoothLeScanner?.stopScan(scanCallback)
        }
    }

    /**
     * The watch advertises rarely — minutes can pass between packets — so a
     * scan that hears nothing is not evidence of anything. Counting everything
     * else on the air is what distinguishes "the watch is quiet" from "we are
     * deaf".
     */
    private fun heartbeat() {
        handler.postDelayed({
            if (scanning) {
                Log.i(TAG, "still scanning, heard ${heard.size} other devices" +
                    if (heard.isEmpty()) " - receiver may be the problem, not the watch" else "")
                heard.clear()
                heartbeat()
            }
        }, HEARTBEAT_MS)
    }

    private val heard = HashSet<String>()

    private val scanCallback = object : ScanCallback() {
        @SuppressLint("MissingPermission")
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            if (!scanning) return
            val name = result.scanRecord?.deviceName
            if (name == null || !name.startsWith(DEVICE_NAME_PREFIX)) {
                heard.add(result.device.address)
                return
            }
            Log.i(TAG, "found '$name' at ${result.device.address} rssi=${result.rssi}, connecting")
            stopScan()
            gatt?.close()
            gatt = result.device.connectGatt(
                this@WatchConnectionService, false, callback, BluetoothDevice.TRANSPORT_LE
            )
        }

        override fun onScanFailed(errorCode: Int) {
            Log.e(TAG, "scan failed errorCode=$errorCode")
            scanning = false
            retryLater("scan failed $errorCode")
        }
    }

    @SuppressLint("MissingPermission")
    private fun retryLater(reason: String) {
        Log.w(TAG, "reconnecting: $reason")
        stopScan()
        gatt?.close()
        gatt = null
        handler.postDelayed({
            val settings = Settings(this)
            val mac = settings.mac ?: return@postDelayed
            val adapter = (getSystemService(Context.BLUETOOTH_SERVICE)
                as android.bluetooth.BluetoothManager).adapter
            connect(adapter, mac)
        }, RETRY_MS)
    }

    private val callback = object : BluetoothGattCallback() {
        @SuppressLint("MissingPermission")
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            Log.i(TAG, "connectionStateChange status=$status newState=$newState")
            when (newState) {
                BluetoothProfile.STATE_CONNECTED -> {
                    WatchRepository.setLink(LinkState.Connected)
                    notify("Connected")
                    g.requestMtu(MTU)
                }
                BluetoothProfile.STATE_DISCONNECTED -> {
                    WatchRepository.setLink(LinkState.Disconnected)
                    notify("Disconnected")
                    channel = null
                    service?.onDisconnected()
                    handler.removeCallbacks(resync)
                    // The address it advertised under may not be reused, so
                    // find it by name again rather than reconnecting blind.
                    retryLater("status=$status")
                }
            }
        }

        @SuppressLint("MissingPermission")
        override fun onMtuChanged(g: BluetoothGatt, mtu: Int, status: Int) {
            Log.i(TAG, "mtu=$mtu status=$status")
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
            WatchRepository.setLink(LinkState.Ready)
            scheduleResync()
            // Only now will the watch's replies actually reach us.
            runCatching { service?.onConnected() }
                .onFailure { Log.e(TAG, "onConnected", it) }
            watchHandshake()
        }

        override fun onCharacteristicChanged(
            g: BluetoothGatt,
            c: BluetoothGattCharacteristic,
            value: ByteArray,
        ) {
            runCatching {
                service?.onBytes(value, System.currentTimeMillis())
                val progress = service?.snapshot()?.progress
                if (progress != lastProgress) {
                    lastProgress = progress
                    Log.i(TAG, "progress=$progress")
                }
            }.onFailure { Log.e(TAG, "onBytes", it) }
        }

        override fun onCharacteristicWrite(g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int) {
            if (status != BluetoothGatt.GATT_SUCCESS) Log.w(TAG, "write failed status=$status")
            writeInFlight = false
            drain()
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

    private inner class GattTransport : Transport {
        override fun write(bytes: ByteArray) {
            synchronized(pending) { pending.addLast(bytes) }
            drain()
        }

        override fun changed() {
            WatchRepository.invalidate()
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
            if (writeInFlight || pending.isEmpty()) null else pending.removeFirst()
        } ?: return
        writeInFlight = true
        g.writeCharacteristic(c, next, BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT)
    }

    @SuppressLint("MissingPermission")
    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        stopScan()
        gatt?.close()
        gatt = null
        super.onDestroy()
    }

    private fun notification(text: String): Notification {
        val manager = getSystemService(NotificationManager::class.java)
        if (manager.getNotificationChannel(CHANNEL_ID) == null) {
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "Watch link", NotificationManager.IMPORTANCE_LOW)
            )
        }
        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("Withoutings")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
            .setOngoing(true)
            .build()
    }

    private fun notify(text: String) {
        getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID, notification(text))
    }

    companion object {
        private const val TAG = "WatchLink"
        private const val CHANNEL_ID = "watch-link"
        private const val NOTIFICATION_ID = 1
        private const val MTU = 512
        private const val RETRY_MS = 10_000L
        private const val HANDSHAKE_MS = 20_000L
        private const val DEVICE_NAME_PREFIX = "ScanWatch"
        private const val HEARTBEAT_MS = 65_000L
        private const val RESYNC_MS = 60_000L
        private const val WITHINGS_SERVICE_MARKER = "5749-5448"
        private const val WITHINGS_CHARACTERISTIC_MARKER = "494E4753"
        private const val PROTOCOL_HANDLE = '4'
        private val CLIENT_CONFIG: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

        fun start(context: Context) {
            context.startForegroundService(Intent(context, WatchConnectionService::class.java))
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, WatchConnectionService::class.java))
        }
    }
}
