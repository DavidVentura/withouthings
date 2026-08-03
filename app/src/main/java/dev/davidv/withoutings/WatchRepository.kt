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

enum class LinkState { Disconnected, Connecting, Connected, Ready }

private fun zone(): Pair<Int, DstChange?> {
    val now = Instant.now()
    val rules = ZoneId.systemDefault().rules
    val next = rules.nextTransition(now)?.let {
        DstChange(it.instant.toEpochMilli(), it.offsetAfter.totalSeconds)
    }
    return rules.getOffset(now).totalSeconds to next
}

fun declareZone(service: WatchService) {
    val (offset, next) = zone()
    service.setZone(offset, next)
}

object WatchRepository {
    private var service: WatchService? = null

    private val _revision = MutableStateFlow(0L)
    val revision: StateFlow<Long> = _revision.asStateFlow()

    private val _link = MutableStateFlow(LinkState.Disconnected)
    val link: StateFlow<LinkState> = _link.asStateFlow()

    private val _listening = MutableStateFlow(false)
    val listening: StateFlow<Boolean> = _listening.asStateFlow()

    fun setListening(on: Boolean) {
        _listening.value = on
    }

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

    fun get(): WatchService? = service
}

class Settings(context: Context) {
    private val prefs = context.getSharedPreferences("withoutings", Context.MODE_PRIVATE)

    val mac: String?
        get() = prefs.getString(KEY_MAC, null)

    val secret: String?
        get() = prefs.getString(KEY_SECRET, null)

    val configured: Boolean
        get() = !mac.isNullOrBlank() && !secret.isNullOrBlank()

    val accountId: UInt
        get() {
            val stored = prefs.getInt(KEY_ACCOUNT_ID, 0)
            if (stored != 0) return stored.toUInt()
            val fresh = (1..Int.MAX_VALUE).random()
            prefs.edit().putInt(KEY_ACCOUNT_ID, fresh).apply()
            return fresh.toUInt()
        }

    val knownWatches: List<KnownWatch>
        get() = JSONObject(prefs.getString(KEY_KNOWN, "{}") ?: "{}").let { stored ->
            stored.keys().asSequence()
                .map { KnownWatch(it, stored.getString(it)) }
                .toList()
        }

    fun remember(mac: String, secret: String) {
        val stored = JSONObject(prefs.getString(KEY_KNOWN, "{}") ?: "{}")
        stored.put(mac.lowercase(), secret)
        prefs.edit().putString(KEY_KNOWN, stored.toString()).apply()
    }

    fun select(mac: String, secret: String) {
        remember(mac, secret)
        prefs.edit().putString(KEY_MAC, mac).putString(KEY_SECRET, secret).apply()
    }

    fun forgetWatch() {
        prefs.edit().remove(KEY_MAC).remove(KEY_SECRET).apply()
    }

    fun forgetWatchAndKey(mac: String) {
        val stored = JSONObject(prefs.getString(KEY_KNOWN, "{}") ?: "{}")
        stored.remove(mac.lowercase())
        prefs.edit().putString(KEY_KNOWN, stored.toString()).apply()
        forgetWatch()
    }

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
