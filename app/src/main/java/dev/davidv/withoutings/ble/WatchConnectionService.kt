package dev.davidv.withoutings.ble

import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.PendingIntent
import android.app.NotificationManager
import android.app.Service
import android.bluetooth.BluetoothAdapter
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.content.Intent
import android.os.IBinder
import android.util.Log
import dev.davidv.withoutings.DbLocation
import dev.davidv.withoutings.LinkState
import dev.davidv.withoutings.Settings
import dev.davidv.withoutings.WatchRepository
import dev.davidv.withoutings.declareZone
import dev.davidv.withoutings.watchDb
import uniffi.wpp_ffi.Progress
import uniffi.wpp_ffi.Transport
import uniffi.wpp_ffi.WatchService

class WatchConnectionService : Service() {

    private var link: GattLink? = null
    private var service: WatchService? = null
    private var ancs: AncsServer? = null
    private var retries = 0
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

        val location = watchDb(this)
        if (location !is DbLocation.Ready) {
            WatchRepository.setLink(LinkState.Disconnected)
            Log.e(TAG, "storage not reachable, no all-files grant")
            stopSelf()
            return START_NOT_STICKY
        }

        if (intent?.action == ACTION_RECONNECT) {
            Log.i(TAG, "reconnect asked for by hand ${transportState()}")
            retryLater("asked to reconnect", Retry.Now)
            return START_STICKY
        }

        if (service == null) {
            val ancs = AncsServer(this) { service }
            this.ancs = ancs
            service = WatchService(
                dbPath = location.path,
                mac = mac,
                secret = secret,
                transport = GattTransport(),
                ancs = ancs,
                rasterizer = AndroidRasterizer(this),
            ).also { WatchRepository.attach(it) }
            service?.preferNotifications(settings.notifications)
            if (settings.notifications) ancs.start()
        }

        if (intent?.action == ACTION_NOTIFICATIONS) {
            val enabled = intent.getBooleanExtra(EXTRA_ENABLED, true)
            Log.i(TAG, "ANCS server ${if (enabled) "on" else "off"}")
            if (enabled) ancs?.start() else ancs?.stop()
        }

        if (link == null) {
            val adapter = (getSystemService(Context.BLUETOOTH_SERVICE) as android.bluetooth.BluetoothManager).adapter
            connect(adapter, mac)
        }
        return START_STICKY
    }

    @SuppressLint("MissingPermission")
    private fun connect(adapter: BluetoothAdapter, mac: String) {
        if (!adapter.isEnabled) {
            WatchRepository.setLink(LinkState.Disconnected)
            Log.e(TAG, "bluetooth is off")
            retryLater("bluetooth is off")
            return
        }
        val scanner = adapter.bluetoothLeScanner
        if (scanner == null) {
            Log.e(TAG, "no BLE scanner")
            retryLater("no BLE scanner")
            return
        }
        if (scanning) {
            Log.i(TAG, "already scanning")
            return
        }

        val bonded = runCatching {
            adapter.bondedDevices.firstOrNull { it.name?.startsWith(DEVICE_NAME_PREFIX) == true }
        }.getOrNull()
        if (bonded != null) {
            Log.i(TAG, "connecting directly to bonded '${bonded.name}' at ${bonded.address}")
            WatchRepository.setLink(LinkState.Connecting)
            openLink().connect(bonded)
            armLinkWatchdog()
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
        handler.removeCallbacks(heartbeat)
        handler.postDelayed(heartbeat, HEARTBEAT_MS)
    }

    private fun scheduleResync() {
        handler.removeCallbacks(resync)
        handler.postDelayed(resync, RESYNC_MS)
    }

    private fun armLinkWatchdog() {
        handler.removeCallbacks(linkWatchdog)
        handler.postDelayed(linkWatchdog, LINK_TIMEOUT_MS)
    }

    private val linkWatchdog = Runnable {
        retryLater("no working link within ${LINK_TIMEOUT_MS / 1000}s")
    }

    private val tick = object : Runnable {
        override fun run() {
            runCatching { service?.tick() }.onFailure { Log.e(TAG, "tick", it) }
            runCatching {
                service?.unhandledObjects()?.forEach { Log.w(TAG, "unread: $it") }
            }.onFailure { Log.e(TAG, "unhandled", it) }
            handler.postDelayed(this, TICK_MS)
        }
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
        scanning = false
        runCatching {
            (getSystemService(Context.BLUETOOTH_SERVICE) as android.bluetooth.BluetoothManager)
                .adapter.bluetoothLeScanner?.stopScan(scanCallback)
        }
    }

    private val heartbeat = object : Runnable {
        override fun run() {
            if (!scanning) return
            Log.i(TAG, "still scanning, heard ${heard.size} other devices" +
                if (heard.isEmpty()) " - receiver may be the problem, not the watch" else "")
            heard.clear()
            handler.postDelayed(this, HEARTBEAT_MS)
        }
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
            openLink().connect(result.device)
            armLinkWatchdog()
        }

        override fun onScanFailed(errorCode: Int) {
            Log.e(TAG, "scan failed errorCode=$errorCode")
            scanning = false
            retryLater("scan failed $errorCode")
        }
    }

    private enum class Retry { Backoff, Now }

    private val retry = Runnable {
        val mac = Settings(this).mac
        if (mac == null) {
            Log.e(TAG, "no watch configured")
            return@Runnable
        }
        val adapter = (getSystemService(Context.BLUETOOTH_SERVICE)
            as android.bluetooth.BluetoothManager).adapter
        connect(adapter, mac)
    }

    @SuppressLint("MissingPermission")
    private fun retryLater(reason: String, pace: Retry = Retry.Backoff) {
        val backoff = when (pace) {
            Retry.Now -> {
                retries = 0
                0L
            }
            Retry.Backoff -> {
                val wait = (RETRY_MS shl retries.coerceAtMost(RETRY_SHIFTS))
                    .coerceAtMost(RETRY_MAX_MS)
                retries++
                wait
            }
        }
        Log.w(TAG, "reconnecting in ${backoff / 1000}s: $reason ${transportState()}")
        handler.removeCallbacks(linkWatchdog)
        handler.removeCallbacks(tick)
        handler.removeCallbacks(retry)
        stopScan()
        link?.close()
        link = null
        runCatching { service?.onDisconnected() }
            .onFailure { Log.e(TAG, "onDisconnected", it) }
        handler.postDelayed(retry, backoff)
    }

    private fun openLink(): GattLink = GattLink(this, listener).also {
        link?.close()
        link = it
    }

    private val listener = object : GattLink.Listener {
        override fun onConnected() {
            WatchRepository.setLink(LinkState.Connected)
            notify("Connected")
        }

        override fun onReady() {
            WatchRepository.setLink(LinkState.Ready)
            scheduleResync()
            handler.removeCallbacks(tick)
            handler.postDelayed(tick, TICK_MS)
            runCatching { service?.onConnected() }
                .onFailure { Log.e(TAG, "onConnected", it) }
            runCatching { service?.let { svc -> declareZone(svc) } }
                .onFailure { Log.w(TAG, "clock sync", it) }
        }

        override fun onBytes(bytes: ByteArray) {
            runCatching {
                retries = 0
                service?.onBytes(bytes, System.currentTimeMillis())
                val progress = service?.progress()
                if (progress != lastProgress) {
                    lastProgress = progress
                    Log.i(TAG, "progress=$progress")
                }
                if (progress != null && progress != Progress.CONNECTING) {
                    handler.removeCallbacks(linkWatchdog)
                }
            }.onFailure { Log.e(TAG, "onBytes", it) }
        }

        override fun onDisconnected(status: Int) {
            WatchRepository.setLink(LinkState.Disconnected)
            notify("Disconnected")
            service?.onDisconnected()
            handler.removeCallbacks(resync)
            retryLater("status=$status")
        }

        override fun onChannelMissing() {
            retryLater("no protocol characteristic")
        }
    }

    private inner class GattTransport : Transport {
        override fun write(frames: List<ByteArray>) {
            link?.write(frames)
        }

        override fun changed() {
            WatchRepository.invalidate()
        }

        override fun reconnect() {
            handler.post { retryLater("the watch stopped answering") }
        }
    }

    private fun transportState(): String = link?.describe() ?: "[no link]"

    @SuppressLint("MissingPermission")
    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        stopScan()
        link?.close()
        link = null
        ancs?.stop()
        ancs = null
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
        private const val ACTION_RECONNECT = "dev.davidv.withoutings.RECONNECT"
        private const val ACTION_NOTIFICATIONS = "dev.davidv.withoutings.NOTIFICATIONS"
        private const val EXTRA_ENABLED = "enabled"
        private const val TAG = "WatchLink"
        private const val CHANNEL_ID = "watch-link"
        private const val NOTIFICATION_ID = 1
        private const val RETRY_MS = 10_000L
        private const val RETRY_MAX_MS = 300_000L
        private const val RETRY_SHIFTS = 5
        private const val LINK_TIMEOUT_MS = 25_000L
        private const val TICK_MS = 30_000L
        private const val DEVICE_NAME_PREFIX = GattLink.DEVICE_NAME_PREFIX
        private const val HEARTBEAT_MS = 65_000L
        private const val RESYNC_MS = 60_000L

        fun start(context: Context) {
            context.startForegroundService(Intent(context, WatchConnectionService::class.java))
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, WatchConnectionService::class.java))
        }

        fun reconnect(context: Context) {
            context.startForegroundService(
                Intent(context, WatchConnectionService::class.java).setAction(ACTION_RECONNECT)
            )
        }

        /**
         * A server that was never opened cannot be subscribed to, so every
         * announcement is dropped while the setting reads as on.
         */
        fun setNotifications(context: Context, enabled: Boolean) {
            context.startForegroundService(
                Intent(context, WatchConnectionService::class.java)
                    .setAction(ACTION_NOTIFICATIONS)
                    .putExtra(EXTRA_ENABLED, enabled)
            )
        }
    }
}
