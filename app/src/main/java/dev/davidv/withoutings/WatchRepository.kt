package dev.davidv.withoutings

import android.content.Context
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
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
 * Watch address and association secret.
 *
 * Falls back to the build config, which is populated from local.properties, so
 * a development build connects without retyping a 32-character key.
 */
class Settings(context: Context) {
    private val prefs = context.getSharedPreferences("withoutings", Context.MODE_PRIVATE)

    var mac: String?
        get() = prefs.getString(KEY_MAC, null) ?: BuildConfig.WATCH_MAC.ifBlank { null }
        set(value) = prefs.edit().putString(KEY_MAC, value).apply()

    var secret: String?
        get() = prefs.getString(KEY_SECRET, null) ?: BuildConfig.WATCH_SECRET.ifBlank { null }
        set(value) = prefs.edit().putString(KEY_SECRET, value).apply()

    val configured: Boolean
        get() = !mac.isNullOrBlank() && !secret.isNullOrBlank()

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
        const val KEY_NOTIFICATIONS = "notifications"
    }
}
