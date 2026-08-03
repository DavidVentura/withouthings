package dev.davidv.withoutings.ble

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.util.Log
import java.security.SecureRandom
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import uniffi.wpp_ffi.KnownWatch
import uniffi.wpp_ffi.PairingProgress
import uniffi.wpp_ffi.PairingService
import uniffi.wpp_ffi.Transport

/** One device heard on the air, as the pairing screen lists it. */
data class Discovered(
    val address: String,
    val name: String?,
    /// Absent for a bonded device, which is listed without having been heard.
    val rssi: Int?,
    /// Advertising under the name the watch family uses. Nothing stronger is
    /// available: the address is random and rotates, and the advertisement
    /// carries no service UUID to match on.
    val isWatch: Boolean,
)

/** Where an attempt to pair with one device has got to. */
sealed interface PairingStage {
    data object Scanning : PairingStage
    data object Connecting : PairingStage
    data object Probing : PairingStage
    data object Associating : PairingStage

    /// A watch we already had a key for; answering its challenge rather than
    /// giving it anything new.
    data object Readopting : PairingStage
    data class Paired(val mac: String, val secret: String) : PairingStage

    /// The watch holds a key that is not one of ours, so it challenged with an
    /// identity we cannot answer for. Whatever paired with it has to release
    /// it first.
    data object AlreadyAssociated : PairingStage
    data class Failed(val reason: String) : PairingStage
}

/**
 * Finding a watch and getting it to accept a key.
 *
 * A singleton rather than something the screen owns: an association in flight
 * must not be torn down and restarted because the screen behind it was
 * recreated, and the watch reboots into a different state depending on how far
 * it got.
 */
object PairingSession {
    private const val TAG = "Pairing"

    /// From connecting to a terminal state. The whole exchange is four frames;
    /// anything slower than this has gone wrong somewhere with no error to say
    /// so, which is what a link at the edge of range looks like.
    private const val TIMEOUT_MS = 30_000L

    /// The characters the official app's keys are drawn from. The watch stores
    /// the key as a C string and hashes it as bytes, so the alphabet only has
    /// to avoid a terminator, but staying inside what the watch has been seen
    /// to hold costs nothing.
    private const val ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

    /// What the watch stores: 32 characters, matching `wpp::pairing::SECRET_LEN`.
    private const val SECRET_LEN = 32

    /// How often the list may be rebuilt. Advertisements arrive far faster
    /// than anyone can read a row, and every one of them reorders the list
    /// under the finger reaching for it.
    private const val PUBLISH_MS = 500L

    /// Signal is sorted in steps this wide. A reading wanders several dBm
    /// between packets from a device that has not moved, and sorting on the
    /// raw number turns that wander into rows swapping places. The number
    /// shown is still the real one.
    private const val RSSI_STEP = 6

    private val _devices = MutableStateFlow<List<Discovered>>(emptyList())
    val devices: StateFlow<List<Discovered>> = _devices.asStateFlow()

    private val _stage = MutableStateFlow<PairingStage>(PairingStage.Scanning)
    val stage: StateFlow<PairingStage> = _stage.asStateFlow()

    private val handler = android.os.Handler(android.os.Looper.getMainLooper())
    private var scanning = false
    private var link: GattLink? = null
    private var service: PairingService? = null
    private val heard = LinkedHashMap<String, Discovered>()

    /// The advertisement's own handle on each device, kept because rebuilding
    /// one from the address string loses the address type — and the watch
    /// advertises under a random one, which a connection made to it as a
    /// public address never reaches.
    private val seen = HashMap<String, BluetoothDevice>()

    /**
     * A fresh key for a fresh association.
     *
     * Generated here rather than in Rust because it is the one value in the
     * protocol that must not be derivable from anything: it is what stands
     * between this watch and the next phone that walks past it.
     */
    private fun generateSecret(): String {
        val random = SecureRandom()
        return (0 until SECRET_LEN)
            .map { ALPHABET[random.nextInt(ALPHABET.length)] }
            .joinToString("")
    }

    @SuppressLint("MissingPermission")
    fun startScan(context: Context) {
        stop()
        _stage.value = PairingStage.Scanning
        handler.removeCallbacks(publish)
        publishPending = false
        heard.clear()
        seen.clear()
        _devices.value = emptyList()
        val adapter = (context.getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager)
            .adapter
        val scanner = adapter?.bluetoothLeScanner
        if (scanner == null) {
            _stage.value = PairingStage.Failed("Bluetooth is off")
            return
        }
        // A watch that has been connected to before is bonded, and a bonded
        // device can be reached without hearing it first. That matters here
        // more than anywhere: this watch advertises rarely enough that waiting
        // for a packet can take minutes, and it has just been disconnected
        // from, which is the one moment it is certainly not advertising yet.
        runCatching { adapter.bondedDevices }.getOrNull().orEmpty()
            .filter { it.name?.startsWith(GattLink.DEVICE_NAME_PREFIX) == true }
            .forEach { note(Discovered(it.address, it.name, null, isWatch = true), it) }
        // No filter: a watch that has never been paired is being looked for by
        // eye, and a filter that misses it looks exactly like a watch that is
        // not there. The list is short enough to read.
        scanning = true
        scanner.startScan(
            emptyList(),
            ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build(),
            scanCallback,
        )
    }

    @SuppressLint("MissingPermission")
    fun stopScan(context: Context) {
        handler.removeCallbacks(publish)
        publishPending = false
        if (!scanning) return
        scanning = false
        runCatching {
            (context.getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager)
                .adapter?.bluetoothLeScanner?.stopScan(scanCallback)
        }
    }

    private val scanCallback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            if (!scanning) return
            val name = result.scanRecord?.deviceName
            note(
                Discovered(
                    address = result.device.address,
                    name = name,
                    rssi = result.rssi,
                    isWatch = name != null && name.startsWith(GattLink.DEVICE_NAME_PREFIX),
                ),
                result.device,
            )
        }

        override fun onScanFailed(errorCode: Int) {
            Log.e(TAG, "scan failed errorCode=$errorCode")
            scanning = false
            _stage.value = PairingStage.Failed("Scan failed ($errorCode)")
        }
    }

    /// Watches first, then anything that gave a name, then by signal. A device
    /// that will not say what it is is not what anyone is looking for, and a
    /// bonded device has no reading at all so it sorts above the noise.
    private val order = compareByDescending<Discovered> { it.isWatch }
        .thenByDescending { it.name != null }
        .thenByDescending { (it.rssi ?: Int.MAX_VALUE).floorDiv(RSSI_STEP) }

    private fun note(device: Discovered, handle: BluetoothDevice) {
        heard[device.address] = device
        seen[device.address] = handle
        if (publishPending) return
        publishPending = true
        handler.postDelayed(publish, PUBLISH_MS)
    }

    private var publishPending = false

    private val publish = Runnable {
        publishPending = false
        // Stable, so devices the ordering cannot separate keep the order they
        // were first heard in rather than shuffling among themselves.
        _devices.value = heard.values.sortedWith(order)
    }

    /**
     * Connect to one device and try to give it a key.
     *
     * `accountId` identifies whoever is claiming the watch. The watch keeps it
     * alongside the key and never asks about it again; it only has to be
     * non-zero, which is how the firmware is told there is an account at all.
     *
     * `known` decides which of the two things happens. A watch that challenges
     * with an identity in there is answered and taken back on untouched; any
     * other watch is either free, and gets the fresh key, or someone else's.
     */
    @SuppressLint("MissingPermission")
    fun pair(context: Context, address: String, accountId: UInt, known: List<KnownWatch>) {
        stopScan(context)
        stop()
        val device = seen[address]
        if (device == null) {
            _stage.value = PairingStage.Failed("That device is no longer being advertised")
            return
        }
        val pairing = runCatching {
            PairingService(generateSecret(), accountId, known, LinkTransport())
        }.getOrElse {
            Log.e(TAG, "pairing service", it)
            _stage.value = PairingStage.Failed(it.message ?: "could not start")
            return
        }
        service = pairing
        _stage.value = PairingStage.Connecting
        handler.postDelayed(timeout, TIMEOUT_MS)
        link = GattLink(context, listener).also { it.connect(device) }
    }

    /**
     * Forget the last attempt, so the next screen starts from scratch.
     *
     * Separate from [stop], which lets go of the link but keeps the answer:
     * the answer is exactly what [stop] must not throw away, since it runs the
     * moment a pairing succeeds and the credentials have yet to be read.
     */
    fun reset() {
        stop()
        heard.clear()
        seen.clear()
        _devices.value = emptyList()
        _stage.value = PairingStage.Scanning
    }

    /// Let go of the link and anything in flight. The scan is separate; a
    /// caller that wants both gone calls [stopScan] too.
    fun stop() {
        handler.removeCallbacks(timeout)
        link?.close()
        link = null
        service = null
    }

    /// Nothing more will happen on this link, one way or the other.
    private fun settled(): Boolean = _stage.value.let {
        it is PairingStage.Paired || it is PairingStage.AlreadyAssociated ||
            it is PairingStage.Failed
    }

    private val timeout = Runnable {
        if (!settled()) {
            Log.w(TAG, "gave up after ${TIMEOUT_MS / 1000}s in ${_stage.value}")
            stop()
            _stage.value = PairingStage.Failed("The watch did not answer")
        }
    }

    private val listener = object : GattLink.Listener {
        override fun onConnected() {}

        override fun onReady() {
            runCatching { service?.onConnected() }
                .onFailure { Log.e(TAG, "onConnected", it) }
        }

        override fun onBytes(bytes: ByteArray) {
            runCatching { service?.onBytes(bytes) }
                .onFailure { Log.e(TAG, "onBytes", it) }
        }

        override fun onDisconnected(status: Int) {
            // The watch drops the link itself once it has stored the key, and
            // we drop it ourselves the moment the answer is in, so a
            // disconnection after either is the end of the exchange rather
            // than something that went wrong during it.
            if (settled()) return
            runCatching { service?.onDisconnected() }
            stop()
            _stage.value = PairingStage.Failed("The link dropped (status $status)")
        }

        override fun onChannelMissing() {
            stop()
            _stage.value = PairingStage.Failed("Not a Withings device: no protocol channel")
        }
    }

    /**
     * The watch's replies drive the state; the transport is only asked to
     * carry bytes and to say when something moved.
     */
    private class LinkTransport : Transport {
        override fun write(bytes: ByteArray) {
            link?.write(bytes)
        }

        override fun changed() {
            val progress = service?.progress() ?: return
            _stage.value = when (progress) {
                is PairingProgress.Idle -> PairingStage.Connecting
                is PairingProgress.Probing -> PairingStage.Probing
                is PairingProgress.Associating -> PairingStage.Associating
                is PairingProgress.Readopting -> PairingStage.Readopting
                is PairingProgress.Paired -> PairingStage.Paired(progress.mac, progress.secret)
                is PairingProgress.AlreadyAssociated -> PairingStage.AlreadyAssociated
            }
            // A peripheral that something is connected to does not advertise,
            // so holding this link would leave the sync service unable to find
            // the watch it was just given the key for. Posted rather than run
            // here: this is the stack's own callback thread, mid-notification.
            if (settled()) handler.post { stop() }
        }

        // Nothing to reconnect to: the association is a single exchange, and a
        // watch that stopped answering halfway is one to start over with by
        // hand rather than to keep dialling.
        override fun reconnect() {}
    }
}
