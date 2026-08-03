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

data class Discovered(
    val address: String,
    val name: String?,
    val rssi: Int?,
    val isWatch: Boolean,
)

sealed interface PairingStage {
    data object Scanning : PairingStage
    data object Connecting : PairingStage
    data object Probing : PairingStage
    data object Associating : PairingStage

    data object Readopting : PairingStage
    data class Paired(val mac: String, val secret: String) : PairingStage

    data object AlreadyAssociated : PairingStage
    data class Failed(val reason: String) : PairingStage
}

object PairingSession {
    private const val TAG = "Pairing"

    private const val TIMEOUT_MS = 30_000L

    /// The watch stores the key as a C string, so a null byte in the alphabet
    /// would silently truncate what gets stored.
    private const val ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

    private const val SECRET_LEN = 32

    private const val PUBLISH_MS = 500L

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

    private val seen = HashMap<String, BluetoothDevice>()

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
        runCatching { adapter.bondedDevices }.getOrNull().orEmpty()
            .filter { it.name?.startsWith(GattLink.DEVICE_NAME_PREFIX) == true }
            .forEach { note(Discovered(it.address, it.name, null, isWatch = true), it) }
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
        _devices.value = heard.values.sortedWith(order)
    }

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

    fun reset() {
        stop()
        heard.clear()
        seen.clear()
        _devices.value = emptyList()
        _stage.value = PairingStage.Scanning
    }

    fun stop() {
        handler.removeCallbacks(timeout)
        link?.close()
        link = null
        service = null
    }

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

    private class LinkTransport : Transport {
        override fun write(frames: List<ByteArray>) {
            link?.write(frames)
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
            if (settled()) handler.post { stop() }
        }

        override fun reconnect() {}
    }
}
