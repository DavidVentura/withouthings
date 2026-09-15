package dev.davidv.withoutings.ui

import android.util.Log
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import dev.davidv.withoutings.LinkState
import dev.davidv.withoutings.WatchRepository
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.filterNotNull
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.mapLatest
import kotlinx.coroutines.flow.scan
import kotlinx.coroutines.flow.stateIn
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
import uniffi.wpp_ffi.ArmedScan
import uniffi.wpp_ffi.HealthFeature
import uniffi.wpp_ffi.Point
import uniffi.wpp_ffi.RouteTrack
import uniffi.wpp_ffi.TrackSummary
import uniffi.wpp_ffi.Travel
import uniffi.wpp_ffi.WearPosition
import uniffi.wpp_ffi.WatchScreen
import uniffi.wpp_ffi.WatchService
import java.io.File
import java.io.RandomAccessFile
import java.security.MessageDigest
import java.util.zip.CRC32
import kotlin.coroutines.coroutineContext
import uniffi.wpp_ffi.WorkoutSummary

sealed interface ActivityEntry {
    val startedAtMs: Long
    val endedAtMs: Long?
    val name: String
    val subcategory: Int
    // Steps and metres climbed come off the pedometer, which describes what the
    // wearer did only when their own feet carried them through it.
    val onFoot: Boolean
    // Whether there could have been a route, which is what tells an activity
    // with no map that nothing was recorded from one that never travels.
    val travel: Travel
    val calories: Double?
}

data class RecordedEntry(val workout: WorkoutSummary) : ActivityEntry {
    override val startedAtMs = workout.startedAtMs
    override val endedAtMs = workout.endedAtMs
    override val name = workout.activity
    override val subcategory = workout.subcategory
    override val onFoot = workout.onFoot
    override val travel = workout.travel
    override val calories = workout.calories
}

data class DetectedEntry(val detected: DetectedActivity) : ActivityEntry {
    override val startedAtMs = detected.startedAtMs
    override val endedAtMs = detected.endedAtMs
    override val name = detected.activity
    override val subcategory = detected.subcategory
    override val onFoot = detected.onFoot
    override val travel = detected.travel
    override val calories = detected.calories
}

/**
 * A session's route, read once and kept: the handle answers questions about
 * where the line goes without reading the fixes again for every frame drawn.
 */
data class Route(
    val track: RouteTrack,
    val summary: TrackSummary,
    val speed: List<ChartPoint>,
    // Metres climbed as the session went on, off the barometer.
    val climb: List<ChartPoint>,
)

data class UiState(
    val link: LinkState = LinkState.Disconnected,
    val snapshot: Snapshot? = null,
    val hr: List<HrPoint> = emptyList(),
    val markers: List<Marker> = emptyList(),
    val activityLog: List<ActivityEntry> = emptyList(),
    val activityLogAtMs: Long = 0,
    val dailySteps: Map<Long, Long> = emptyMap(),
    val screens: List<WatchScreen> = emptyList(),
    val latest: Map<MetricStyle, ChartPoint> = emptyMap(),
    val wearPosition: WearPosition = WearPosition.NOT_SET,
    val activities: List<Activity> = emptyList(),
    val features: List<HealthFeature> = emptyList(),
    val respiratoryScan: ArmedScan? = null,
    val notifications: NotificationConfig? = null,
    val hrWindow: LongRange = 0L..0L,
    val workoutTemp: List<ChartPoint> = emptyList(),
    val liveRoute: Route? = null,
    val liveRouteAtMs: Long = 0,
    val ecgs: List<EcgSummary> = emptyList(),
    val liveEcg: List<Double> = emptyList(),
    val home: HomeState = HomeState(),
)

data class HomeState(
    val hr: List<ChartPoint> = emptyList(),
    val temperature: List<ChartPoint> = emptyList(),
    val respiratory: List<ChartPoint> = emptyList(),
    val fortnightHr: List<ChartPoint> = emptyList(),
    val fortnightTemperature: List<ChartPoint> = emptyList(),
    val sleep: SleepSpans = SleepSpans.none,
    val lastNight: Night? = null,
    val today: List<ActivityEntry> = emptyList(),
    val distanceMetres: Double? = null,
    val calories: Double? = null,
    val builtAtMs: Long = 0,
)

const val MS_TO_KMH = 3.6

private const val DEFAULT_WINDOW_MS = 10 * 60 * 1000L

private const val DETECTED_HISTORY_MS = 7L * DAY_MS

private const val ACTIVITY_LOG_MAX_AGE_MS = 10_000L

// The screen refreshes four times a second while a session runs. Re-reading
// and re-cleaning a whole ride at that rate would be the most expensive thing
// on the path, and a distance does not visibly change in ten seconds.
private const val LIVE_ROUTE_MAX_AGE_MS = 10_000L

private const val HOME_MAX_AGE_MS = 10_000L

private const val DETECTED_HISTORY_DAYS = 7

private const val COUNTER_ROLLOVER_MS = 5 * 60 * 1000L

private const val BASELINE_DAYS = 14

private const val BASELINE_MS = BASELINE_DAYS * DAY_MS

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

sealed interface FlashDumpState {
    data object Idle : FlashDumpState
    data class Testing(val message: String) : FlashDumpState
    data class Dumping(
        val done: Long,
        val total: Long,
        val path: String,
        val startedAtMs: Long,
        val startOffset: Long,
    ) : FlashDumpState
    data class Done(val message: String) : FlashDumpState
    data class Failed(val reason: String) : FlashDumpState
}

private const val TAG = "WatchViewModel"

private fun spanEndingNow(spanMs: Long): LongRange {
    val now = System.currentTimeMillis()
    return (now - spanMs)..now
}

class WatchViewModel : ViewModel() {

    private val _state = MutableStateFlow(UiState())
    val state: StateFlow<UiState> = _state.asStateFlow()

    private var refreshing = false
    private var refreshAgain = false

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

    private val _selectedRoute = MutableStateFlow<Route?>(null)
    val selectedRoute: StateFlow<Route?> = _selectedRoute.asStateFlow()

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

    private val _metricWindow = MutableStateFlow(spanEndingNow(MetricStyle.HeartRate.defaultSpan))
    val metricWindow: StateFlow<LongRange> = _metricWindow.asStateFlow()

    private data class MetricRequest(
        val style: MetricStyle,
        val load: LoadWindow,
        val revision: Long,
    )

    @OptIn(ExperimentalCoroutinesApi::class)
    val metricSeries: StateFlow<MetricSeries> = run {
        val initial = MetricRequest(_metricStyle.value, loadWindow(_metricWindow.value), -1L)
        combine(_metricStyle, _metricWindow, WatchRepository.revision, ::Triple)
            .scan(initial) { previous, (style, window, revision) ->
                if (previous.style != style || previous.revision != revision) {
                    return@scan MetricRequest(style, loadWindow(window), revision)
                }
                val reload = reloadFor(previous.load, window) ?: return@scan previous
                MetricRequest(style, reload, revision)
            }
            .distinctUntilChanged()
            .mapLatest { request ->
                withContext(Dispatchers.IO) { runCatching { loadMetric(request) }.getOrNull() }
            }
            .filterNotNull()
            .stateIn(
                viewModelScope,
                SharingStarted.Eagerly,
                MetricSeries(initial.style, initial.load),
            )
    }

    private fun loadMetric(request: MetricRequest): MetricSeries? {
        val service = WatchRepository.get() ?: return null
        val range = request.load.range
        val style = request.style
        val now = System.currentTimeMillis()
        // The figures a window is read against are the fortnight's, whichever
        // stretch of history the chart itself is showing.
        val known = minOf(range.first, now - BASELINE_MS)..maxOf(range.last, now)
        return MetricSeries(
            style = style,
            load = request.load,
            points = service
                .series(style.metric, range.first, range.last, LOAD_POINTS)
                .map { p: Point -> ChartPoint(p.atMs, p.value) },
            baseline = if (style.accumulates) {
                emptyList()
            } else {
                service
                    .series(style.metric, now - BASELINE_MS, now, MAX_CHART_POINTS)
                    .map { p: Point -> ChartPoint(p.atMs, p.value) }
            },
            sleep = if (style.comparesModes) {
                sleepSpans(service, known.first, known.last)
            } else {
                SleepSpans.none
            },
            dailyTotals = if (style.accumulates) {
                dailyTotals(service, style, daysCovering(known), now)
            } else {
                emptyMap()
            },
            charging = if (style == MetricStyle.Battery) {
                service.charging(range.first, range.last)
            } else {
                emptyList()
            },
        )
    }

    private val _save = MutableStateFlow<SaveState>(SaveState.Idle)
    val save: StateFlow<SaveState> = _save.asStateFlow()

    private val _flashDump = MutableStateFlow<FlashDumpState>(FlashDumpState.Idle)
    val flashDump: StateFlow<FlashDumpState> = _flashDump.asStateFlow()
    private var flashJob: Job? = null

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
        if (refreshing) {
            refreshAgain = true
            return
        }
        refreshing = true
        viewModelScope.launch {
            val next = withContext(Dispatchers.IO) {
                runCatching { buildState(service) }.getOrNull()
            }
            if (next != null) _state.value = next
            refreshing = false
            if (refreshAgain) {
                refreshAgain = false
                refresh()
            }
        }
    }

    private fun buildState(service: WatchService): UiState {
        val snapshot = service.snapshot()
        val active = snapshot.activeWorkout
        val now = System.currentTimeMillis()
        val range = _window.value ?: followSpanMs?.let { (now - it)..now } ?: run {
            val from = active?.startedAtMs ?: (now - DEFAULT_WINDOW_MS)
            from..now
        }
        val previous = _state.value
        val rebuildLog = now - previous.activityLogAtMs > ACTIVITY_LOG_MAX_AGE_MS
        val rebuildRoute = now - previous.liveRouteAtMs > LIVE_ROUTE_MAX_AGE_MS
        val liveRoute = when {
            active == null -> null
            !rebuildRoute -> previous.liveRoute
            else -> runCatching { route(service, RecordedEntry(active), active.startedAtMs..now) }
                .onFailure { Log.w(TAG, "live route: unreadable", it) }
                .getOrNull()
        }
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
        val steps = if (rebuildLog) {
            (0..DETECTED_HISTORY_DAYS).associate { back ->
                val day = dayStart(now - back * DAY_MS)
                day to service.activityTotals(day, day + DAY_MS - 1000).steps
            }
        } else {
            previous.dailySteps
        }
        return UiState(
            link = WatchRepository.link.value,
            snapshot = snapshot,
            hrWindow = range,
            hr = service.hrSeries(range.first, range.last, MAX_CHART_POINTS),
            workoutTemp = service
                .series(Metric.TEMPERATURE, range.first, range.last, MAX_CHART_POINTS)
                .map { p: Point -> ChartPoint(p.atMs, p.value) },
            markers = service.markers(range.first, range.last),
            liveRoute = liveRoute,
            liveRouteAtMs = if (active != null && rebuildRoute) now else previous.liveRouteAtMs,
            activityLog = log,
            activityLogAtMs = if (rebuildLog) now else previous.activityLogAtMs,
            dailySteps = steps,
            home = home(service, log, previous.home, now),
            ecgs = service.ecgs(),
            liveEcg = if (snapshot.measuring) {
                service.liveEcg()
            } else {
                previous.liveEcg
            },
            screens = service.screens(),
            wearPosition = service.wearPosition(),
            activities = service.activities(),
            features = service.healthFeatures(),
            respiratoryScan = service.respiratoryScan(),
            notifications = service.notificationConfig(),
            latest = MetricStyle.entries.mapNotNull { entry ->
                service.latestValue(entry.metric)
                    ?.let { entry to ChartPoint(it.atMs, it.value) }
            }.toMap(),
        )
    }

    private fun dailyTotals(
        service: WatchService,
        style: MetricStyle,
        days: List<Long>,
        nowMs: Long,
    ): Map<Long, Double> {
        val counted = days.filter { it <= nowMs }
        if (counted.isEmpty()) return emptyMap()
        // The watch keeps reporting yesterday's total for the first minutes of a
        // day, so a day's count runs from its own rollover to the next one.
        val found = service.windowedMax(
            style.metric,
            counted.map { it + COUNTER_ROLLOVER_MS } + (counted.last() + DAY_MS),
        )
        return counted.zip(found)
            .mapNotNull { (day, total) -> total?.let { day to it } }
            .toMap()
    }

    private fun sleepSpans(service: WatchService, fromMs: Long, toMs: Long): SleepSpans =
        SleepSpans.of(service.sleepSpans(fromMs, toMs).map { Span(it.fromMs, it.toMs) })

    private fun home(
        service: WatchService,
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
            sleep = sleepSpans(service, fortnightAgo, nowMs),
            lastNight = runCatching { service.night(nightRange.first, nightRange.last) }.getOrNull(),
            today = log.filter { it.startedAtMs >= midnight },
            distanceMetres = service.latestValue(Metric.DISTANCE)
                ?.takeIf { it.atMs >= midnight }?.value,
            calories = service.latestValue(Metric.CALORIES)
                ?.takeIf { it.atMs >= midnight }?.value,
            builtAtMs = nowMs,
        )
    }

    fun showMetric(style: MetricStyle) {
        _metricStyle.value = style
        _metricWindow.value = spanEndingNow(style.defaultSpan)
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
    }

    fun metricRangeSpan(span: Long) {
        _metricWindow.value = spanEndingNow(span)
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
        _selectedRoute.value = null
        val service = WatchRepository.get() ?: return
        viewModelScope.launch {
            _selectedTotals.value = withContext(Dispatchers.IO) {
                runCatching { service.activityTotals(span.first, span.last) }.getOrNull()
            }
        }
        viewModelScope.launch {
            _selectedRoute.value = withContext(Dispatchers.IO) {
                runCatching { route(service, entry, span) }
                    .onFailure { Log.w(TAG, "route: unreadable", it) }
                    .getOrNull()
            }
        }
    }

    private fun route(service: WatchService, entry: ActivityEntry, span: LongRange): Route? {
        val track = service.track(span.first, span.last, entry.subcategory) ?: return null
        return Route(
            track = track,
            summary = track.summary(),
            // Metres a second is what a receiver reports; nobody reads a ride
            // in them.
            speed = track.speedSeries().map { ChartPoint(it.atMs, it.value * MS_TO_KMH) },
            climb = service.ascentSeries(span.first, span.last)
                .map { ChartPoint(it.atMs, it.value) },
        )
    }

    fun trimActivity(entry: RecordedEntry, endedAtMs: Long) {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch {
            val trimmed = withContext(Dispatchers.IO) {
                runCatching { service.trimWorkout(entry.workout.id, endedAtMs) }
            }.onFailure { Log.w(TAG, "trim: refused", it) }.getOrNull() ?: return@launch
            _state.value = _state.value.copy(activityLogAtMs = 0)
            showActivity(RecordedEntry(trimmed))
        }
    }

    fun deleteActivity(entry: RecordedEntry) {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch {
            withContext(Dispatchers.IO) { runCatching { service.deleteWorkout(entry.workout.id) } }
                .onFailure { Log.w(TAG, "delete: refused", it) }
            _selectedActivity.value = null
            _selectedTotals.value = null
            _selectedRoute.value = null
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

    // Arming is an action rather than an edit: it takes effect on its own and
    // does not close the screen the way the save footer does.
    fun setRespiratoryScan(armed: Boolean) {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch {
            withContext(Dispatchers.IO) {
                runCatching {
                    if (armed) service.armRespiratoryScan() else service.cancelRespiratoryScan()
                }.onFailure { Log.w(TAG, "respiratory scan: the write failed", it) }
            }
            refresh()
        }
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

    /**
     * Debug: reads the fwblk table at the active bank and reports enough of its
     * decode to prove the bytes are the real structured table and not noise —
     * the SHA-256, the table version (1), the appl fw_version, and whether the
     * trailing CRC32 validates.
     */
    fun testFlashRead() {
        val service = WatchRepository.get() ?: return
        if (flashJob?.isActive == true) return
        flashJob = viewModelScope.launch(Dispatchers.IO) {
            _flashDump.value = FlashDumpState.Testing("Reading fwblk table at 0x6000…")
            val bytes = try {
                readBlock(service, FWBLK_ACTIVE.toUInt(), FWBLK_PROBE_LEN.toUInt())
            } catch (c: CancellationException) {
                _flashDump.value = FlashDumpState.Idle
                throw c
            } catch (e: Exception) {
                _flashDump.value = FlashDumpState.Failed("Test read failed: ${e.message}")
                return@launch
            }
            _flashDump.value = FlashDumpState.Done(describeFwblk(bytes))
        }
    }

    /**
     * Debug: reads the whole 8 MB external flash into [file], one block at a
     * time so a long dump survives an interruption. A file already there is
     * resumed from its last whole block rather than restarted.
     */
    fun dumpFlash(file: File) {
        val service = WatchRepository.get() ?: return
        if (flashJob?.isActive == true) return
        flashJob = viewModelScope.launch(Dispatchers.IO) {
            try {
                runFlashDump(service, file)
            } catch (c: CancellationException) {
                _flashDump.value = FlashDumpState.Failed(
                    "Cancelled at ${file.length()} bytes — press Dump to resume"
                )
                throw c
            } catch (e: Exception) {
                _flashDump.value = FlashDumpState.Failed(
                    "Failed at ${file.length()} bytes (${e.message}) — press Dump to resume"
                )
            }
        }
    }

    fun cancelFlashDump() {
        flashJob?.cancel()
    }

    private suspend fun runFlashDump(service: WatchService, file: File) {
        val path = file.absolutePath
        // Resume from a whole-block boundary; a half-written trailing block is
        // dropped so the file only ever holds bytes that were fully read.
        var offset = (file.length() / FLASH_BLOCK) * FLASH_BLOCK
        if (offset >= FLASH_SIZE) offset = 0L
        val startedAtMs = System.currentTimeMillis()
        val startOffset = offset
        RandomAccessFile(file, "rw").use { out ->
            out.setLength(offset)
            out.seek(offset)
            _flashDump.value = FlashDumpState.Dumping(offset, FLASH_SIZE, path, startedAtMs, startOffset)
            while (offset < FLASH_SIZE) {
                coroutineContext.ensureActive()
                val len = minOf(FLASH_BLOCK, FLASH_SIZE - offset)
                val block = readBlock(service, offset.toUInt(), len.toUInt())
                if (block.size.toLong() != len) {
                    throw IllegalStateException(
                        "short block at 0x${offset.toString(16)}: got ${block.size}, want $len"
                    )
                }
                out.write(block)
                offset += len
                _flashDump.value = FlashDumpState.Dumping(offset, FLASH_SIZE, path, startedAtMs, startOffset)
            }
        }
        _flashDump.value = FlashDumpState.Done("Dumped $FLASH_SIZE bytes to $path")
    }

    /**
     * One block of the address space, resent until the watch delivers it. A
     * dropped link or a stalled read is met by waiting for the link and asking
     * again; the whole block is re-requested since a partial one is discarded.
     */
    private suspend fun readBlock(service: WatchService, addr: UInt, len: UInt): ByteArray {
        var attempt = 0
        while (true) {
            coroutineContext.ensureActive()
            if (WatchRepository.link.value != LinkState.Ready) {
                withTimeoutOrNull(RECONNECT_WAIT_MS) {
                    WatchRepository.link.first { it == LinkState.Ready }
                }
            }
            service.spiFlashRead(addr, len)
            val bytes = awaitBlock(service)
            if (bytes != null) return bytes
            attempt++
            if (attempt >= BLOCK_ATTEMPTS) {
                throw IllegalStateException(
                    "no answer for $len bytes at 0x${addr.toString(16)} after $attempt tries"
                )
            }
        }
    }

    private suspend fun awaitBlock(service: WatchService): ByteArray? {
        val startedAt = System.currentTimeMillis()
        while (true) {
            coroutineContext.ensureActive()
            // A null progress means the read was reset out from under us (the
            // link dropped); the caller resends the block.
            val progress = service.spiFlashProgress() ?: return null
            progress.error?.let {
                throw IllegalStateException("watch refused the read (err $it)")
            }
            if (progress.done) return service.spiFlashTake() ?: ByteArray(0)
            if (System.currentTimeMillis() - startedAt > BLOCK_TIMEOUT_MS) return null
            delay(FLASH_POLL_MS)
        }
    }

    private fun describeFwblk(bytes: ByteArray): String {
        val sha = MessageDigest.getInstance("SHA-256").digest(bytes)
            .joinToString("") { "%02x".format(it) }
        fun u16(at: Int) = (bytes[at].toInt() and 0xff) or ((bytes[at + 1].toInt() and 0xff) shl 8)
        fun u32(at: Int) =
            (0..3).fold(0L) { acc, i -> acc or ((bytes[at + i].toLong() and 0xff) shl (8 * i)) }

        val version = u16(0)
        val bodyLen = u16(2)
        val crcAt = 4 + bodyLen
        val decodable = crcAt + 4 <= bytes.size
        val storedCrc = if (decodable) u32(crcAt) else -1L
        val computedCrc = if (decodable) {
            CRC32().apply { update(bytes, 0, crcAt) }.value
        } else {
            -1L
        }
        var pos = 4
        var applVersion: Long? = null
        while (decodable && pos + 4 <= crcAt) {
            val ieId = u16(pos)
            val size = u16(pos + 2)
            if (ieId == FWBLK_IE_APPL && size >= 16) applVersion = u32(pos + 4 + 12)
            pos += 4 + size
        }
        return buildString {
            appendLine("Read OK: ${bytes.size} bytes at 0x${FWBLK_ACTIVE.toString(16)}")
            appendLine("SHA-256: $sha")
            appendLine("fwblk version: $version (expect 1)")
            appendLine("appl fw_version: ${applVersion ?: "not found"}")
            append(
                when {
                    !decodable -> "table CRC32: body length $bodyLen overruns the probe"
                    computedCrc == storedCrc -> "table CRC32: ok"
                    else -> "table CRC32: MISMATCH " +
                        "(calc 0x${computedCrc.toString(16)}, stored 0x${storedCrc.toString(16)})"
                }
            )
        }
    }

    private companion object {
        const val MAX_CHART_POINTS = 1200u
        const val TEST_APP_ID = "dev.davidv.withoutings"

        const val FLASH_SIZE = 0x800000L
        const val FLASH_BLOCK = 0x4000L
        const val FWBLK_ACTIVE = 0x6000L
        const val FWBLK_PROBE_LEN = 512L
        const val FWBLK_IE_APPL = 1
        const val FLASH_POLL_MS = 40L
        const val BLOCK_TIMEOUT_MS = 20_000L
        const val BLOCK_ATTEMPTS = 8
        const val RECONNECT_WAIT_MS = 60_000L
    }
}
