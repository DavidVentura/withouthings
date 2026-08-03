package dev.davidv.withoutings.ui

import android.util.Log
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import dev.davidv.withoutings.LinkState
import dev.davidv.withoutings.WatchRepository
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import uniffi.wpp_ffi.DetectedActivity
import uniffi.wpp_ffi.NotificationCategory
import uniffi.wpp_ffi.NotificationConfig
import uniffi.wpp_ffi.EcgRecording
import uniffi.wpp_ffi.EcgSummary
import uniffi.wpp_ffi.HrPoint
import uniffi.wpp_ffi.Marker
import uniffi.wpp_ffi.Metric
import uniffi.wpp_ffi.Night
import uniffi.wpp_ffi.Snapshot
import uniffi.wpp_ffi.Activity
import uniffi.wpp_ffi.ActivityTotals
import uniffi.wpp_ffi.HealthFeature
import uniffi.wpp_ffi.Point
import uniffi.wpp_ffi.WearPosition
import uniffi.wpp_ffi.WatchScreen
import uniffi.wpp_ffi.WatchService
import uniffi.wpp_ffi.WorkoutSummary

sealed interface ActivityEntry {
    val startedAtMs: Long
    val endedAtMs: Long?
    val name: String
}

data class RecordedEntry(val workout: WorkoutSummary) : ActivityEntry {
    override val startedAtMs = workout.startedAtMs
    override val endedAtMs = workout.endedAtMs
    override val name = workout.activity
}

data class DetectedEntry(val detected: DetectedActivity) : ActivityEntry {
    override val startedAtMs = detected.startedAtMs
    override val endedAtMs = detected.endedAtMs
    override val name = detected.activity
}

data class UiState(
    val link: LinkState = LinkState.Disconnected,
    val snapshot: Snapshot? = null,
    val hr: List<HrPoint> = emptyList(),
    val markers: List<Marker> = emptyList(),
    val activityLog: List<ActivityEntry> = emptyList(),
    val activityLogAtMs: Long = 0,
    val screens: List<WatchScreen> = emptyList(),
    val metric: List<ChartPoint> = emptyList(),
    val latest: Map<MetricStyle, ChartPoint> = emptyMap(),
    val wearPosition: WearPosition = WearPosition.NOT_SET,
    val activities: List<Activity> = emptyList(),
    val features: List<HealthFeature> = emptyList(),
    val notifications: NotificationConfig? = null,
    val hrWindow: LongRange = 0L..0L,
    val workoutTemp: List<ChartPoint> = emptyList(),
    val ecgs: List<EcgSummary> = emptyList(),
    val liveEcg: List<Double> = emptyList(),
    val charging: List<Marker> = emptyList(),
    val metricBaseline: List<ChartPoint> = emptyList(),
    val home: HomeState = HomeState(),
)

data class HomeState(
    val hr: List<ChartPoint> = emptyList(),
    val temperature: List<ChartPoint> = emptyList(),
    val respiratory: List<ChartPoint> = emptyList(),
    val fortnightHr: List<ChartPoint> = emptyList(),
    val fortnightTemperature: List<ChartPoint> = emptyList(),
    val lastNight: Night? = null,
    val today: List<ActivityEntry> = emptyList(),
    val distanceMetres: Double? = null,
    val calories: Double? = null,
    val builtAtMs: Long = 0,
)

private const val DEFAULT_WINDOW_MS = 10 * 60 * 1000L

private const val DETECTED_HISTORY_MS = 7L * 24 * 60 * 60 * 1000

private const val ACTIVITY_LOG_MAX_AGE_MS = 10_000L

private const val HOME_MAX_AGE_MS = 10_000L

private const val BASELINE_MS = 14L * 24 * 60 * 60 * 1000

private const val INITIAL_ECG_SPAN_MS = 6_000L

private const val CHARGE_POLL_MS = 15_000L

private const val MAX_NIGHT_SEARCH_DAYS = 400

private const val RESET_TIMEOUT_MS = 20_000L

private const val ACK_TIMEOUT_MS = 8_000L
private const val ACK_POLL_MS = 250L
private const val ACK_REASK_MS = 1_500L

internal const val NOTIFICATION_FEATURE: UShort = 19u

sealed interface SaveState {
    data object Idle : SaveState
    data object Saving : SaveState
    data object Saved : SaveState
    data class Failed(val reason: String) : SaveState
}

private const val TAG = "WatchViewModel"

class WatchViewModel : ViewModel() {

    private val _state = MutableStateFlow(UiState())
    val state: StateFlow<UiState> = _state.asStateFlow()

    private val _window = MutableStateFlow<LongRange?>(null)
    val window: StateFlow<LongRange?> = _window.asStateFlow()

    private val _ecg = MutableStateFlow<EcgRecording?>(null)
    val ecg: StateFlow<EcgRecording?> = _ecg.asStateFlow()

    private val _ecgWindow = MutableStateFlow<LongRange?>(null)
    val ecgWindow: StateFlow<LongRange?> = _ecgWindow.asStateFlow()

    private val _liveWindow = MutableStateFlow<LongRange?>(null)
    val liveWindow: StateFlow<LongRange?> = _liveWindow.asStateFlow()

    private val _selectedActivity = MutableStateFlow<ActivityEntry?>(null)
    val selectedActivity: StateFlow<ActivityEntry?> = _selectedActivity.asStateFlow()

    private val _selectedTotals = MutableStateFlow<ActivityTotals?>(null)
    val selectedTotals: StateFlow<ActivityTotals?> = _selectedTotals.asStateFlow()

    private val _night = MutableStateFlow<Night?>(null)
    val night: StateFlow<Night?> = _night.asStateFlow()

    private val _testNotification = MutableStateFlow<UInt?>(null)
    val testNotification: StateFlow<UInt?> = _testNotification.asStateFlow()

    private val _nightsAgo = MutableStateFlow(0)

    private val _nightWindow = MutableStateFlow<LongRange?>(null)
    val nightWindow: StateFlow<LongRange?> = _nightWindow.asStateFlow()

    private var followSpanMs: Long? = null

    private val _metricStyle = MutableStateFlow(MetricStyle.HeartRate)
    val metricStyle: StateFlow<MetricStyle> = _metricStyle.asStateFlow()

    private val _metricWindow = MutableStateFlow<LongRange?>(null)
    val metricWindow: StateFlow<LongRange?> = _metricWindow.asStateFlow()

    private val _save = MutableStateFlow<SaveState>(SaveState.Idle)
    val save: StateFlow<SaveState> = _save.asStateFlow()

    private val _stopwatchStartedAt = MutableStateFlow<Long?>(null)
    val stopwatchStartedAt: StateFlow<Long?> = _stopwatchStartedAt.asStateFlow()

    private val _elapsed = MutableStateFlow(0L)
    val elapsed: StateFlow<Long> = _elapsed.asStateFlow()

    init {
        viewModelScope.launch {
            WatchRepository.revision.collect { refresh() }
        }
        viewModelScope.launch {
            while (true) {
                delay(250)
                _stopwatchStartedAt.value?.let { _elapsed.value = System.currentTimeMillis() - it }
                val live = _state.value.snapshot
                if (live?.activeWorkout != null || live?.measuring == true) refresh()
            }
        }
    }

    fun refresh() {
        val service = WatchRepository.get()
        if (service == null) {
            _state.value = _state.value.copy(link = WatchRepository.link.value)
            return
        }
        viewModelScope.launch {
            val next = withContext(Dispatchers.IO) {
                runCatching {
                    val snapshot = service.snapshot()
                    val active = snapshot.activeWorkout
                    val now = System.currentTimeMillis()
                    val range = _window.value ?: followSpanMs?.let { (now - it)..now } ?: run {
                        val from = active?.startedAtMs ?: (now - DEFAULT_WINDOW_MS)
                        from..now
                    }
                    val previous = _state.value
                    val rebuildLog = now - previous.activityLogAtMs > ACTIVITY_LOG_MAX_AGE_MS
                    val log = if (rebuildLog) {
                        (
                            service.workouts(50u).map(::RecordedEntry) +
                                service
                                    .detectedActivities(now - DETECTED_HISTORY_MS, now)
                                    .map(::DetectedEntry)
                            ).sortedByDescending { it.startedAtMs }
                    } else {
                        previous.activityLog
                    }
                    UiState(
                        link = WatchRepository.link.value,
                        snapshot = snapshot,
                        hrWindow = range,
                        hr = service.hrSeries(range.first, range.last, MAX_CHART_POINTS),
                        workoutTemp = service
                            .series(Metric.TEMPERATURE, range.first, range.last, MAX_CHART_POINTS)
                            .map { p: Point -> ChartPoint(p.atMs, p.value) },
                        markers = service.markers(range.first, range.last),
                        activityLog = log,
                        activityLogAtMs = if (rebuildLog) now else previous.activityLogAtMs,
                        home = home(service, log, previous.home, now),
                        ecgs = service.ecgs(),
                        liveEcg = if (snapshot.measuring) {
                            service.liveEcg()
                        } else {
                            _state.value.liveEcg
                        },
                        screens = service.screens(),
                        wearPosition = service.wearPosition(),
                        activities = service.activities(),
                        features = service.healthFeatures(),
                        notifications = service.notificationConfig(),
                        metric = service
                            .series(
                                _metricStyle.value.metric,
                                metricRange().first,
                                metricRange().last,
                                MAX_CHART_POINTS,
                            )
                            .map { p: Point -> ChartPoint(p.atMs, p.value) },
                        charging = if (_metricStyle.value == MetricStyle.Battery) {
                            service.charging(metricRange().first, metricRange().last)
                        } else {
                            emptyList()
                        },
                        metricBaseline = service
                            .series(
                                _metricStyle.value.metric,
                                now - BASELINE_MS,
                                now,
                                MAX_CHART_POINTS,
                            )
                            .map { p: Point -> ChartPoint(p.atMs, p.value) },
                        latest = MetricStyle.entries.mapNotNull { style ->
                            service.latestValue(style.metric)
                                ?.let { style to ChartPoint(it.atMs, it.value) }
                        }.toMap(),
                    )
                }.getOrNull()
            }
            if (next != null) _state.value = next
        }
    }

    private fun home(
        service: uniffi.wpp_ffi.WatchService,
        log: List<ActivityEntry>,
        previous: HomeState,
        nowMs: Long,
    ): HomeState {
        if (nowMs - previous.builtAtMs < HOME_MAX_AGE_MS) return previous
        val midnight = dayStart(nowMs)
        val fortnightAgo = nowMs - BASELINE_MS

        fun series(metric: Metric, fromMs: Long) = service
            .series(metric, fromMs, nowMs, MAX_CHART_POINTS)
            .map { p: Point -> ChartPoint(p.atMs, p.value) }

        val nightRange = nightRangeFor(0)
        return HomeState(
            hr = series(Metric.HEART_RATE, midnight),
            temperature = series(Metric.TEMPERATURE, midnight),
            respiratory = series(Metric.RESPIRATORY_RATE, midnight),
            fortnightHr = series(Metric.HEART_RATE, fortnightAgo),
            fortnightTemperature = series(Metric.TEMPERATURE, fortnightAgo),
            lastNight = runCatching { service.night(nightRange.first, nightRange.last) }.getOrNull(),
            today = log.filter { it.startedAtMs >= midnight },
            distanceMetres = service.latestValue(Metric.DISTANCE)
                ?.takeIf { it.atMs >= midnight }?.value,
            calories = service.latestValue(Metric.CALORIES)
                ?.takeIf { it.atMs >= midnight }?.value,
            builtAtMs = nowMs,
        )
    }

    private fun metricRange(): LongRange {
        _metricWindow.value?.let { return it }
        val now = System.currentTimeMillis()
        return (now - _metricStyle.value.defaultSpan)..now
    }

    fun showMetric(style: MetricStyle) {
        _metricStyle.value = style
        _metricWindow.value = null
        refresh()
    }

    private fun nightRange(): LongRange = nightRangeFor(_nightsAgo.value)

    private fun nightRangeFor(daysAgo: Int): LongRange {
        val midnight = dayStart(System.currentTimeMillis()) - daysAgo * 86_400_000L
        return (midnight - 6 * 3600_000L)..(midnight + 12 * 3600_000L)
    }

    fun showNight() {
        val service = WatchRepository.get() ?: return
        val range = nightRange()
        _nightWindow.value = range
        viewModelScope.launch {
            val loaded = withContext(Dispatchers.IO) {
                runCatching { service.night(range.first, range.last) }.getOrNull()
            }
            _night.value = loaded
            _nightWindow.value = loaded?.sleepWindow() ?: range
        }
    }

    fun shiftNight(by: Int) {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch {
            val found = withContext(Dispatchers.IO) {
                var days = _nightsAgo.value
                repeat(MAX_NIGHT_SEARCH_DAYS) {
                    val next = days + by
                    if (next < 0) return@withContext null
                    days = next
                    val range = nightRangeFor(days)
                    val staged = runCatching { service.hasStaging(range.first, range.last) }
                        .getOrDefault(false)
                    if (staged) return@withContext days
                }
                null
            }
            if (found != null) {
                _nightsAgo.value = found
                showNight()
            }
        }
    }

    fun nightZoom(range: LongRange) {
        _nightWindow.value = range
    }

    fun metricZoom(range: LongRange) {
        _metricWindow.value = range
        refresh()
    }

    fun metricRangeSpan(span: Long) {
        val now = System.currentTimeMillis()
        _metricWindow.value = (now - span)..now
        refresh()
    }

    fun zoom(range: LongRange?) {
        if (range != null) followSpanMs = range.last - range.first
        _window.value = range
        refresh()
    }

    fun showEcg(id: Long) {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch {
            val loaded = withContext(Dispatchers.IO) { runCatching { service.ecg(id) }.getOrNull() }
            _ecg.value = loaded
            _ecgWindow.value = loaded?.let {
                it.measuredAtMs..(it.measuredAtMs + INITIAL_ECG_SPAN_MS)
            }
        }
    }

    fun ecgZoom(range: LongRange) {
        _ecgWindow.value = range
    }

    fun liveEcgZoom(range: LongRange) {
        _liveWindow.value = range
    }

    fun showActivity(entry: ActivityEntry) {
        _selectedActivity.value = entry
        val span = entry.startedAtMs..(entry.endedAtMs ?: System.currentTimeMillis())
        zoom(span)
        _selectedTotals.value = null
        val service = WatchRepository.get() ?: return
        viewModelScope.launch {
            _selectedTotals.value = withContext(Dispatchers.IO) {
                runCatching { service.activityTotals(span.first, span.last) }.getOrNull()
            }
        }
    }

    fun deleteActivity(entry: RecordedEntry) {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch {
            withContext(Dispatchers.IO) { runCatching { service.deleteWorkout(entry.workout.id) } }
                .onFailure { Log.w(TAG, "delete: refused", it) }
            _selectedActivity.value = null
            _selectedTotals.value = null
            _state.value = _state.value.copy(activityLogAtMs = 0)
            refresh()
        }
    }

    fun followLive() {
        _window.value = null
        refresh()
    }

    fun toggleStopwatch() {
        val service = WatchRepository.get() ?: return
        val now = System.currentTimeMillis()
        val started = _stopwatchStartedAt.value
        viewModelScope.launch(Dispatchers.IO) {
            runCatching {
                if (started == null) {
                    service.markSet(now, uniffi.wpp_ffi.SetEdge.START)
                } else {
                    service.markSet(now, uniffi.wpp_ffi.SetEdge.END)
                }
            }
        }
        _stopwatchStartedAt.value = if (started == null) now else null
        _elapsed.value = 0
    }

    private var chargeWatch: Job? = null

    fun watchCharging(on: Boolean) {
        chargeWatch?.cancel()
        chargeWatch = if (!on) null else viewModelScope.launch {
            while (true) {
                withContext(Dispatchers.IO) {
                    runCatching { WatchRepository.get()?.pollBattery() }
                }
                delay(CHARGE_POLL_MS)
            }
        }
    }


    fun requestScreens() {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch(Dispatchers.IO) { runCatching { service.requestScreens() } }
    }

    fun applyScreens(ids: ByteArray) {
        val wanted = ids.map { it.toUByte() }
        save(
            send = { it.setScreens(ids) },
            reask = { it.requestScreens() },
            confirm = { it.screens().filter { s -> s.enabled }.map { s -> s.id } == wanted },
            unconfirmed = "The watch did not come back with the new order.",
        )
    }

    fun applyActivities(ids: List<UInt>) {
        Log.i(TAG, "quick launch: sending $ids")
        save(
            send = { it.setActivities(ids) },
            reask = { it.requestDeviceConfig() },
            confirm = { it.activities().filter { a -> a.enabled }.map { a -> a.id } == ids },
            unconfirmed = "The watch did not come back with the new menu.",
        )
    }

    fun applyFeatures(changes: List<Pair<UShort, Boolean>>) {
        save(
            send = { service -> changes.forEach { (id, on) -> service.setHealthFeature(id, on) } },
            reask = null,
            confirm = null,
            unconfirmed = "",
        )
    }

    fun applyUser(birthSecs: Long, weightGrams: UInt, heightCm: UInt) {
        save(
            send = { it.setUser(birthSecs, weightGrams, heightCm) },
            reask = { it.requestDeviceConfig() },
            confirm = {
                val held = it.snapshot().user
                held != null &&
                    held.birthSecs == birthSecs &&
                    held.weightGrams == weightGrams &&
                    held.heightCm == heightCm
            },
            unconfirmed = "The watch did not come back with the new profile.",
        )
    }

    private fun save(
        send: (WatchService) -> Unit,
        reask: ((WatchService) -> Unit)?,
        confirm: ((WatchService) -> Boolean)?,
        unconfirmed: String,
    ) {
        val service = WatchRepository.get()
        if (service == null) {
            _save.value = SaveState.Failed("Not connected to the watch.")
            return
        }
        _save.value = SaveState.Saving
        viewModelScope.launch {
            val refusal = withContext(Dispatchers.IO) {
                runCatching { send(service) }
                    .onFailure { Log.w(TAG, "save: the write itself failed", it) }
                    .exceptionOrNull()
            }
            if (refusal != null) {
                _save.value = SaveState.Failed(refusal.message ?: "The write failed.")
                return@launch
            }
            if (confirm == null) {
                _save.value = SaveState.Saved
                return@launch
            }
            val agreed = withTimeoutOrNull(ACK_TIMEOUT_MS) {
                var sinceAsked = 0L
                while (!withContext(Dispatchers.IO) {
                        runCatching { confirm(service) }.getOrDefault(false)
                    }
                ) {
                    if (sinceAsked <= 0) {
                        withContext(Dispatchers.IO) { runCatching { reask?.invoke(service) } }
                        sinceAsked = ACK_REASK_MS
                    }
                    delay(ACK_POLL_MS)
                    sinceAsked -= ACK_POLL_MS
                }
                true
            } == true
            if (!agreed) {
                Log.w(TAG, "save: no confirmation in ${ACK_TIMEOUT_MS}ms")
                runCatching {
                    Log.w(
                        TAG,
                        "save: watch holds " +
                            service.activities().filter { it.enabled }.map { it.id },
                    )
                    Log.w(TAG, "save: screens ${service.screens().filter { it.enabled }}")
                }
            }
            _save.value = if (agreed) SaveState.Saved else SaveState.Failed(unconfirmed)
        }
    }

    fun acknowledgeSave() {
        _save.value = SaveState.Idle
    }

    fun requestDeviceConfig() {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch(Dispatchers.IO) { runCatching { service.requestDeviceConfig() } }
    }

    fun startWorkout(activity: UInt) {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch(Dispatchers.IO) {
            runCatching { service.startWorkout(activity) }
                .onFailure { Log.w(TAG, "start workout $activity", it) }
        }
    }

    fun stopWorkout() {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch(Dispatchers.IO) {
            runCatching { service.stopWorkout() }.onFailure { Log.w(TAG, "stop workout", it) }
        }
    }

    fun setWearPosition(position: WearPosition) {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch(Dispatchers.IO) { runCatching { service.setWearPosition(position) } }
    }


    /**
     * Forgetting the key here before the watch has acted on the reset locks the
     * app out of a watch it can no longer authenticate to, so [onForgotten]
     * must only run once the reset is confirmed.
     */
    fun unpair(onForgotten: () -> Unit) {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch {
            val sent = withContext(Dispatchers.IO) {
                runCatching { service.factoryReset() }.isSuccess
            }
            if (!sent) return@launch
            withTimeoutOrNull(RESET_TIMEOUT_MS) {
                WatchRepository.link.first { it == LinkState.Disconnected }
            }
            onForgotten()
        }
    }

    fun requestRefresh() {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch(Dispatchers.IO) { runCatching { service.requestRefresh() } }
    }

    fun setNotifications(enabled: Boolean) {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch(Dispatchers.IO) {
            runCatching {
                service.setNotifications(enabled)
                service.setHealthFeature(NOTIFICATION_FEATURE, enabled)
            }
        }
    }

    fun postTestNotification() {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch(Dispatchers.IO) {
            runCatching {
                // Dismiss before posting, or the previous id is lost and the
                // watch is left holding a notification nothing can clear.
                _testNotification.value?.let { service.dismissNotification(it) }
                val id = service.postNotification(
                    appId = "$TEST_APP_ID.t${System.currentTimeMillis() % 100000}",
                    title = "",
                    subtitle = "",
                    message = "ABC ${probeGlyph()} ABC",
                    category = NotificationCategory.SOCIAL,
                )
                _testNotification.value = id
            }
        }
    }

    private fun probeGlyph(): Char =
        ('Ａ'.code + (System.currentTimeMillis() / 1000 % 26).toInt()).toChar()

    fun dismissTestNotification() {
        val service = WatchRepository.get() ?: return
        val id = _testNotification.value ?: return
        viewModelScope.launch(Dispatchers.IO) {
            runCatching { service.dismissNotification(id) }
            _testNotification.value = null
        }
    }

    private companion object {
        const val MAX_CHART_POINTS = 1200u
        const val TEST_APP_ID = "dev.davidv.withoutings"
    }
}
