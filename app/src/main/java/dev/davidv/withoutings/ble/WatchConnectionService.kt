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
import dev.davidv.withoutings.LinkState
import dev.davidv.withoutings.Settings
import dev.davidv.withoutings.WatchRepository
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

    private var link: GattLink? = null
    private var service: WatchService? = null
    private var ancs: AncsServer? = null
    /// Consecutive failed attempts, cleared once a link carries real traffic.
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

        if (intent?.action == ACTION_RECONNECT) {
            Log.i(TAG, "reconnect asked for by hand ${transportState()}")
            retryLater("asked to reconnect", Retry.Now)
            return START_STICKY
        }

        if (service == null) {
            // The watch reads notifications out of a server we run, so it has
            // to be listening before the link comes up rather than after the
            // first notification is posted.
            val ancs = AncsServer(this) { service }
            this.ancs = ancs
            service = WatchService(
                dbPath = getDatabasePath("watch.db").also { it.parentFile?.mkdirs() }.absolutePath,
                mac = mac,
                secret = secret,
                transport = GattTransport(),
                ancs = ancs,
                rasterizer = AndroidRasterizer(this),
            ).also { WatchRepository.attach(it) }
            // Told before the link comes up, so the watch is corrected on the
            // first pass rather than after a notification has been missed.
            service?.preferNotifications(settings.notifications)
            if (settings.notifications) ancs.start()
        }

        if (link == null) {
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
        // Both of these are transient — the adapter comes back — so they have
        // to leave a retry behind. Returning bare strands the service for good.
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
        // Android rejects a second scan on the same callback, and the refusal
        // arrives as a failure that looks like the first scan dying.
        if (scanning) {
            Log.i(TAG, "already scanning")
            return
        }

        // A connected peripheral does not advertise, so if anything still holds
        // the link — including a GATT client left behind by a killed process —
        // scanning can never find it. Bonded devices can be reached directly.
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

    /**
     * A finished sync is only current as of when it finished, and the watch
     * only volunteers a sync request sometimes, so walk again on a timer.
     */
    private fun scheduleResync() {
        handler.removeCallbacks(resync)
        handler.postDelayed(resync, RESYNC_MS)
    }

    /**
     * Everything between deciding to connect and having a working protocol
     * link, on one deadline.
     *
     * A connection attempt at the edge of range can produce no GATT callback
     * at all — neither connected nor disconnected — and the handshake that
     * follows has no timeout of its own either. Both failures look identical
     * from outside: "connecting", forever, until the app is restarted.
     */
    private fun armLinkWatchdog() {
        handler.removeCallbacks(linkWatchdog)
        handler.postDelayed(linkWatchdog, LINK_TIMEOUT_MS)
    }

    private val linkWatchdog = Runnable {
        retryLater("no working link within ${LINK_TIMEOUT_MS / 1000}s")
    }

    /**
     * The client has no clock of its own; every interval it enforces is
     * measured against the last thing it heard. Without this it cannot tell a
     * quiet watch from a stopped one, and its own rate limits freeze exactly
     * when the link goes wrong.
     */
    private val tick = object : Runnable {
        override fun run() {
            runCatching { service?.tick() }.onFailure { Log.e(TAG, "tick", it) }
            // The watch sent it, we decoded it, and nothing read it. Silence
            // here once cost ten nights of sleep staging.
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
        // Unconditional: the flag tracks our intent, not what the Bluetooth
        // stack has registered, and those disagree after a failed start.
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

    /// Whether a retry waits out the backoff or goes now. Asking by hand is
    /// evidence that something changed, which the backoff cannot know.
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

    /**
     * Tear the link down and try again later.
     *
     * Reachable from six places, several of which can fire for the same
     * failure, so the retry is a named callback that replaces any already
     * scheduled. As a lambda it could not be cancelled, and each caller added
     * another connection attempt to the same moment.
     *
     * The delay grows while attempts keep failing: a watch that is off or out
     * of range is not coming back within ten seconds, and each attempt is radio
     * work. It resets as soon as a link actually carries traffic.
     */
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

    /**
     * The link's half of the conversation. Everything the protocol needs is
     * behind [GattLink]; what is left here is the service's own bookkeeping —
     * the watchdog, the retry counter and what the notification says.
     */
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
        }

        override fun onBytes(bytes: ByteArray) {
            runCatching {
                retries = 0
                service?.onBytes(bytes, System.currentTimeMillis())
                // Not snapshot(): that reads the database, on the thread
                // delivering notifications, thousands of them back to back
                // during a sync. A notification we are too slow to take is a
                // frame lost, and frames span notifications.
                val progress = service?.progress()
                if (progress != lastProgress) {
                    lastProgress = progress
                    Log.i(TAG, "progress=$progress")
                }
                // The link is doing its job; stop counting against it.
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
            // The address it advertised under may not be reused, so find it by
            // name again rather than reconnecting blind.
            retryLater("status=$status")
        }

        override fun onChannelMissing() {
            retryLater("no protocol characteristic")
        }
    }

    private inner class GattTransport : Transport {
        override fun write(bytes: ByteArray) {
            link?.write(bytes)
        }

        override fun changed() {
            WatchRepository.invalidate()
        }

        // The client decided the watch has stopped answering; only the shell
        // can do anything about it.
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

        /**
         * Drop the link and build a new one, now.
         *
         * The service outlives the activity, so closing the app leaves a stuck
         * link exactly as stuck. Short of force-stopping the process from
         * Android's own settings there was no way back from one.
         */
        fun reconnect(context: Context) {
            context.startForegroundService(
                Intent(context, WatchConnectionService::class.java).setAction(ACTION_RECONNECT)
            )
        }
    }
}
