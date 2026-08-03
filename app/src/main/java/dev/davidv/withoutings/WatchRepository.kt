package dev.davidv.withoutings

import android.content.Context
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.json.JSONObject
import java.time.Instant
import java.time.ZoneId
import uniffi.wpp_ffi.DstChange
import uniffi.wpp_ffi.KnownWatch
import uniffi.wpp_ffi.WatchService

/**
 * The single handle on the Rust side.
 *
 * [revision] ticks whenever Rust reports something changed; the UI re-queries
 * rather than receiving pushed state, so there is one source of truth and no
 * cache to fall out of date.
 */
/**
 * State of the BLE link. Owned by Kotlin, which holds the GATT; Rust never
 * sees it, so it does not belong in the snapshot.
 */
enum class LinkState { Disconnected, Connecting, Connected, Ready }

/**
 * Put the watch's clock right.
 *
 * The watch times everything it records by its own clock and nothing corrects
 * it on its own, so a drift is not confined to the timestamps — a workout
 * stopped from here is ended with this phone's clock and measured against a
 * start stamped with the watch's, which turns the gap between them into
 * duration. The official app sends this on every connection and keeps the
 * watch within a second of the phone.
 *
 * The zone rules are the host's to know, which is why this lives here rather
 * than in the client.
 */
fun syncWatchClock(service: WatchService) {
    val now = Instant.now()
    val rules = ZoneId.systemDefault().rules
    val next = rules.nextTransition(now)?.let {
        DstChange(it.instant.toEpochMilli(), it.offsetAfter.totalSeconds)
    }
    service.setTime(now.toEpochMilli(), rules.getOffset(now).totalSeconds, next)
}

object WatchRepository {
    private var service: WatchService? = null

    private val _revision = MutableStateFlow(0L)
    val revision: StateFlow<Long> = _revision.asStateFlow()

    private val _link = MutableStateFlow(LinkState.Disconnected)
    val link: StateFlow<LinkState> = _link.asStateFlow()

    fun setLink(state: LinkState) {
        _link.value = state
        invalidate()
    }

    fun attach(service: WatchService) {
        this.service = service
        invalidate()
    }

    fun invalidate() {
        _revision.value += 1
    }

    /** Null until the connection service has started. */
    fun get(): WatchService? = service
}

/**
 * Which watch the app is on, and the keys it holds.
 *
 * Everything here is the app's own state. A watch becomes reachable by being
 * paired with and by nothing else, so a build carries no watch in it and two
 * installs of the same build know different watches.
 */
class Settings(context: Context) {
    private val prefs = context.getSharedPreferences("withoutings", Context.MODE_PRIVATE)

    /// The watch in use, by the identity it challenges under. Null when none
    /// has been paired with, or after unpairing.
    val mac: String?
        get() = prefs.getString(KEY_MAC, null)

    val secret: String?
        get() = prefs.getString(KEY_SECRET, null)

    val configured: Boolean
        get() = !mac.isNullOrBlank() && !secret.isNullOrBlank()

    /**
     * Who the watch is told is claiming it, kept so a re-pair of the same
     * watch presents the same account rather than looking like a new one.
     *
     * The official app sends a Withings user id here; this app has no account,
     * so the number only has to be stable and non-zero — zero is how the watch
     * is told to store the key without an account beside it.
     */
    val accountId: UInt
        get() {
            val stored = prefs.getInt(KEY_ACCOUNT_ID, 0)
            if (stored != 0) return stored.toUInt()
            val fresh = (1..Int.MAX_VALUE).random()
            prefs.edit().putInt(KEY_ACCOUNT_ID, fresh).apply()
            return fresh.toUInt()
        }

    /**
     * Every watch a key is held for, by the identity it challenges under.
     *
     * Kept apart from the selected one so that letting go of a watch does not
     * have to mean losing the only thing that can ever authenticate to it. A
     * watch whose key is still here is taken back on by answering its
     * challenge, which asks nothing of the watch and changes nothing on it.
     */
    val knownWatches: List<KnownWatch>
        get() = JSONObject(prefs.getString(KEY_KNOWN, "{}") ?: "{}").let { stored ->
            stored.keys().asSequence()
                .map { KnownWatch(it, stored.getString(it)) }
                .toList()
        }

    /// Keep a watch's key without selecting it.
    fun remember(mac: String, secret: String) {
        val stored = JSONObject(prefs.getString(KEY_KNOWN, "{}") ?: "{}")
        stored.put(mac.lowercase(), secret)
        prefs.edit().putString(KEY_KNOWN, stored.toString()).apply()
    }

    /// Take a watch on: it becomes the selected one, and its key is kept
    /// whether or not it stays selected.
    fun select(mac: String, secret: String) {
        remember(mac, secret)
        prefs.edit().putString(KEY_MAC, mac).putString(KEY_SECRET, secret).apply()
    }

    /// Put the watch down, keeping its key.
    fun forgetWatch() {
        prefs.edit().remove(KEY_MAC).remove(KEY_SECRET).apply()
    }

    /**
     * Put it down and drop its key too, for a watch that has been erased.
     *
     * Keeping the key of a watch that no longer holds it is worse than useless
     * — it would be offered on every later scan and refused every time.
     */
    fun forgetWatchAndKey(mac: String) {
        val stored = JSONObject(prefs.getString(KEY_KNOWN, "{}") ?: "{}")
        stored.remove(mac.lowercase())
        prefs.edit().putString(KEY_KNOWN, stored.toString()).apply()
        forgetWatch()
    }

    /// Phone notifications, which are ANCS and nothing else.
    ///
    /// Off is worth about half the watch's idle radio: it only asks for the
    /// faster connection parameters in order to run ANCS discovery, and never
    /// asks for the slower ones back.
    var notifications: Boolean
        get() = prefs.getBoolean(KEY_NOTIFICATIONS, true)
        set(value) = prefs.edit().putBoolean(KEY_NOTIFICATIONS, value).apply()

    private companion object {
        const val KEY_MAC = "mac"
        const val KEY_SECRET = "secret"
        const val KEY_ACCOUNT_ID = "account-id"
        const val KEY_KNOWN = "known-watches"
        const val KEY_NOTIFICATIONS = "notifications"
    }
}
