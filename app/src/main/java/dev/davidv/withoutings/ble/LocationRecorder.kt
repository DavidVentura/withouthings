package dev.davidv.withoutings.ble

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.location.LocationRequest
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import java.util.concurrent.Executor
import kotlin.math.roundToInt
import uniffi.wpp_ffi.LocationFix
import uniffi.wpp_ffi.WatchService

/**
 * The watch has no receiver of its own, so a route exists only because the
 * phone was carried along. Runs for as long as a session that covers ground
 * does, and not a second longer: at a fix a second this is the most expensive
 * thing the app ever asks the phone for.
 */
class LocationRecorder(context: Context, private val service: () -> WatchService?) {

    private val context = context.applicationContext
    private val manager = context.getSystemService(LocationManager::class.java)
    private var recording = false

    // A fix a second, each one a write to the database. Taking them on a
    // thread of their own keeps that off whatever is drawing the screen.
    private var thread: HandlerThread? = null

    fun arm() {
        if (recording) return
        if (!permitted(context)) {
            Log.w(TAG, "no location permission, no route")
            return
        }
        val provider = provider()
        if (provider == null) {
            Log.w(TAG, "location is switched off on this phone, no route")
            return
        }
        val own = HandlerThread(TAG).apply { start() }
        val handler = Handler(own.looper)
        // The provider-and-interval overload forces high accuracy for the gps
        // provider and leaves every other one at balanced power, which lets the
        // platform serve a route from cell and wifi.
        val request = LocationRequest.Builder(INTERVAL_MS)
            .setQuality(LocationRequest.QUALITY_HIGH_ACCURACY)
            .setMinUpdateIntervalMillis(INTERVAL_MS)
            .build()
        val started = runCatching {
            manager.requestLocationUpdates(provider, request, Executor(handler::post), listener)
        }
        // Android refuses a while-in-use permission to a service that did not
        // take the location type while it was allowed to. Nothing here can put
        // that right; the session goes unrecorded and the link carries on.
        if (started.isFailure) {
            Log.e(TAG, "location updates refused", started.exceptionOrNull())
            own.quitSafely()
            return
        }
        thread = own
        recording = true
        Log.i(TAG, "recording a route from $provider at ${INTERVAL_MS}ms")
        tell(true)
    }

    // Answering a status query is what moves the watch's screens into the mode
    // that will show the phone's figures, and a session begun on the watch has
    // never asked for one.
    private fun tell(present: Boolean) {
        val watch = service()
        if (watch == null) {
            Log.w(TAG, "no service to tell about the receiver")
            return
        }
        runCatching {
            if (present) watch.refreshWorkoutStatus()
            watch.setGpsStatus(present)
        }.onFailure { Log.e(TAG, "could not set the watch's gps status", it) }
    }

    fun disarm() {
        if (!recording) return
        recording = false
        tell(false)
        runCatching { manager.removeUpdates(listener) }
            .onFailure { Log.e(TAG, "could not stop location updates", it) }
        thread?.quitSafely()
        thread = null
        Log.i(TAG, "stopped recording")
    }

    /**
     * The platform's own fusion where there is one, which folds the receiver
     * together with the phone's other sensors and generally holds a line
     * better between buildings than raw satellite fixes do. It is part of
     * AOSP rather than Play Services, so nothing is dragged in behind it, but
     * not every phone carries one.
     */
    private fun provider(): String? {
        if (manager.isProviderEnabled(LocationManager.FUSED_PROVIDER)) {
            return LocationManager.FUSED_PROVIDER
        }
        if (manager.isProviderEnabled(LocationManager.GPS_PROVIDER)) {
            return LocationManager.GPS_PROVIDER
        }
        return null
    }

    private val listener = LocationListener { location ->
        val watch = service()
        if (watch == null) {
            Log.w(TAG, "a fix arrived with no service to store it")
            return@LocationListener
        }
        runCatching { watch.recordFixes(listOf(location.fix())) }
            .onFailure { Log.e(TAG, "could not store a fix", it) }
        runCatching { watch.pushRouteToWatch() }
            .onFailure { Log.e(TAG, "could not push the route to the watch", it) }
    }

    companion object {
        private const val TAG = "Route"
        private const val INTERVAL_MS = 1000L

        fun permitted(context: Context): Boolean =
            context.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) ==
                PackageManager.PERMISSION_GRANTED
    }
}

/**
 * Degrees to the seven decimal places the rows are stored at, and every other
 * field to the integer unit it is kept in. A receiver reports what it has, so
 * a field it did not fix is absent rather than zero.
 */
private fun Location.fix(): LocationFix = LocationFix(
    atMs = time,
    latE7 = (latitude * 1e7).roundToInt(),
    lonE7 = (longitude * 1e7).roundToInt(),
    altitudeCm = if (hasAltitude()) (altitude * 100).roundToInt() else null,
    accuracyCm = if (hasAccuracy()) (accuracy * 100).roundToInt() else null,
    speedMmS = if (hasSpeed()) (speed * 1000).roundToInt() else null,
    bearingCdeg = if (hasBearing()) (bearing * 100).roundToInt() else null,
)
