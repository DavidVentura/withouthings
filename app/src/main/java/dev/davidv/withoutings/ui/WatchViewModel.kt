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
import java.time.Instant
import java.time.ZoneId
import uniffi.wpp_ffi.DetectedActivity
import uniffi.wpp_ffi.DstChange
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
import uniffi.wpp_ffi.HealthFeature
import uniffi.wpp_ffi.Point
import uniffi.wpp_ffi.WearPosition
import uniffi.wpp_ffi.WatchScreen
import uniffi.wpp_ffi.WatchService
import uniffi.wpp_ffi.WorkoutSummary

/// An entry in the activities list. The watch records what someone starts on
/// it; everything else it only counts, and walks have to be found in those
/// counts afterwards.
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
    /// When the list was last built, so a refresh can tell a stale one from a
    /// list that has simply not been rebuilt yet.
    val activityLogAtMs: Long = 0,
    val screens: List<WatchScreen> = emptyList(),
    val metric: List<ChartPoint> = emptyList(),
    val latest: Map<MetricStyle, ChartPoint> = emptyMap(),
    val wearPosition: WearPosition = WearPosition.NOT_SET,
    val activities: List<Activity> = emptyList(),
    val features: List<HealthFeature> = emptyList(),
    /// Null until the watch has been asked about phone notifications.
    val notifications: NotificationConfig? = null,
    /// The window the heart-rate trace was actually fetched over, so the chart
    /// draws the same range the data came from.
    val hrWindow: LongRange = 0L..0L,
    val workoutTemp: List<ChartPoint> = emptyList(),
    val ecgs: List<EcgSummary> = emptyList(),
    val liveEcg: List<Double> = emptyList(),
    /// Charging periods over the metric window, shaded behind the battery.
    val charging: List<Marker> = emptyList(),
    /// A fortnight of the metric on screen, which is what its personal band and
    /// its own-history delta are read off. Never plotted: the chart shows the
    /// selected window, and this is only the yardstick beside it.
    val metricBaseline: List<ChartPoint> = emptyList(),
    val home: HomeState = HomeState(),
)

/**
 * What the two home screens are made of.
 *
 * Gathered apart from the rest because it is expensive — a fortnight of two
 * series plus a night's staging — and because none of it changes between the
 * four-times-a-second polls behind a live workout.
 */
data class HomeState(
    /// Midnight to now, which is what the day ribbon draws and what every
    /// figure on Now is derived from.
    val hr: List<ChartPoint> = emptyList(),
    val temperature: List<ChartPoint> = emptyList(),
    val respiratory: List<ChartPoint> = emptyList(),
    /// The same two over a fortnight, for "3 below your fortnight average".
    val fortnightHr: List<ChartPoint> = emptyList(),
    val fortnightTemperature: List<ChartPoint> = emptyList(),
    val lastNight: Night? = null,
    /// Everything recorded since local midnight, newest first.
    val today: List<ActivityEntry> = emptyList(),
    /// Distance and energy the watch counted for today, if it has yet.
    val distanceMetres: Double? = null,
    val calories: Double? = null,
    val builtAtMs: Long = 0,
)

private const val DEFAULT_WINDOW_MS = 10 * 60 * 1000L

/// How far back the activities list looks for walks. The segmentation runs
/// over every window in the span each time the list is built, so this is a
/// cost as much as it is a horizon.
private const val DETECTED_HISTORY_MS = 7L * 24 * 60 * 60 * 1000

/// How stale the activities list may get. Only a sync can change it, and those
/// are a quarter of an hour apart.
private const val ACTIVITY_LOG_MAX_AGE_MS = 10_000L

/// How stale the home bundle may get. Same reasoning as the activities list,
/// and it is rebuilt on the same pass.
private const val HOME_MAX_AGE_MS = 10_000L

/// The stretch of a person's own past that the app compares today against.
/// A fortnight is long enough to cover a routine and short enough that a
/// change in one is not averaged away.
private const val BASELINE_MS = 14L * 24 * 60 * 60 * 1000

/// Six seconds is what a clinical strip shows on one line at 25 mm/s.
private const val INITIAL_ECG_SPAN_MS = 6_000L

/// Frequent enough that plugging in or unplugging shows up before you look
/// away. Nothing on the wire announces either — only a full battery is pushed
/// — so this is the only way to see a charge start, and every poll is a wake
/// for the watch.
private const val CHARGE_POLL_MS = 15_000L

/**
 * How far back a night step will look for one the watch staged. Longer than the
 * watch's own history, so the search ends because there is nothing left rather
 * than because it gave up.
 */
private const val MAX_NIGHT_SEARCH_DAYS = 400

/// How long to wait for the watch to erase itself and reboot before giving up
/// on hearing the link die. Erasing takes a moment; a link that outlives this
/// never got the command.
private const val RESET_TIMEOUT_MS = 20_000L

/// How long to wait for the watch to confirm a set, and how often to ask.
///
/// Asking matters more than waiting. The read that confirms a write is a
/// command like any other, and one sent while the watch is still storing a
/// menu goes unanswered — so the wait re-asks rather than sitting on a reply
/// that never came. With that, a few seconds is plenty; without it, no amount
/// of waiting helps.
private const val ACK_TIMEOUT_MS = 8_000L
private const val ACK_POLL_MS = 250L
private const val ACK_REASK_MS = 1_500L

/// `FEATURE_ID_NOTIFICATION`. Not a sensor, and not a switch of its own — it
/// is moved by [WatchViewModel.setNotifications] along with the ANCS config.
internal const val NOTIFICATION_FEATURE: UShort = 19u

/**
 * How a page-sized edit is going.
 *
 * Sent and taken are different things, so they are different states: the
 * button stays disabled through [Saving] and the page only leaves on [Saved].
 */
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

    /** Chart viewport; null means "follow the live edge". */
    private val _window = MutableStateFlow<LongRange?>(null)
    val window: StateFlow<LongRange?> = _window.asStateFlow()

    /// The recording open in the ECG viewer, with its samples.
    private val _ecg = MutableStateFlow<EcgRecording?>(null)
    val ecg: StateFlow<EcgRecording?> = _ecg.asStateFlow()

    private val _ecgWindow = MutableStateFlow<LongRange?>(null)
    val ecgWindow: StateFlow<LongRange?> = _ecgWindow.asStateFlow()

    /// Null while a recording is in progress, when the view follows the newest
    /// sample instead of staying where it was put.
    private val _liveWindow = MutableStateFlow<LongRange?>(null)
    val liveWindow: StateFlow<LongRange?> = _liveWindow.asStateFlow()

    /// The finished activity being looked at, if one was picked from the list.
    private val _selectedActivity = MutableStateFlow<ActivityEntry?>(null)
    val selectedActivity: StateFlow<ActivityEntry?> = _selectedActivity.asStateFlow()

    private val _night = MutableStateFlow<Night?>(null)
    val night: StateFlow<Night?> = _night.asStateFlow()

    /// The test notification currently on the watch, by the id that dismisses
    /// it. Null when there is none to clear.
    private val _testNotification = MutableStateFlow<UInt?>(null)
    val testNotification: StateFlow<UInt?> = _testNotification.asStateFlow()

    /// How many nights back the sleep screen is looking; 0 is last night.
    private val _nightsAgo = MutableStateFlow(0)

    private val _nightWindow = MutableStateFlow<LongRange?>(null)
    val nightWindow: StateFlow<LongRange?> = _nightWindow.asStateFlow()

    /// How wide the view was when it was last set by hand. Returning to the
    /// live edge keeps this rather than snapping back to the whole workout.
    private var followSpanMs: Long? = null

    /// Which series the detail screen is showing, and over what window.
    private val _metricStyle = MutableStateFlow(MetricStyle.HeartRate)
    val metricStyle: StateFlow<MetricStyle> = _metricStyle.asStateFlow()

    private val _metricWindow = MutableStateFlow<LongRange?>(null)
    val metricWindow: StateFlow<LongRange?> = _metricWindow.asStateFlow()

    /// How a page-sized edit is going, for the one button that sends it.
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
                // A live workout keeps producing samples without any protocol
                // event to hang a refresh on, so the chart polls while running.
                val live = _state.value.snapshot
                if (live?.activeWorkout != null || live?.measuring == true) refresh()
            }
        }
    }

    fun refresh() {
        val service = WatchRepository.get()
        if (service == null) {
            // Link state is worth showing before the service exists; that is
            // exactly when it says "not connected".
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
                    // Rebuilding the list re-reads a week of activity windows,
                    // which the four-times-a-second poll behind a live workout
                    // does not need: a walk cannot appear before the watch has
                    // synced the windows it is made of.
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
                        // Over the same window as the trace, so a past workout
                        // opened from the list carries its temperature too.
                        workoutTemp = service
                            .series(Metric.TEMPERATURE, range.first, range.last, MAX_CHART_POINTS)
                            .map { p: Point -> ChartPoint(p.atMs, p.value) },
                        markers = service.markers(range.first, range.last),
                        activityLog = log,
                        activityLogAtMs = if (rebuildLog) now else previous.activityLogAtMs,
                        home = home(service, log, previous.home, now),
                        ecgs = service.ecgs(),
                        // The client holds the samples until the next
                        // recording starts, so a finished one stays readable;
                        // refetching it forever would just copy it again.
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

    /**
     * The home bundle, rebuilt no more often than it can change.
     *
     * A fortnight of two series and a night's staging is far too much to
     * re-read behind a live workout, which polls four times a second; nothing
     * in here can move faster than a sync anyway.
     */
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

    /// The window a night is looked for in: evening through to late morning.
    ///
    /// It reaches well past both ends of any sleep because the detection takes
    /// its levels from what it is given — a window holding only sleep has
    /// nothing to measure the sleep against.
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
            // Only once the night is loaded is there a sleep period to frame
            // it on; until then the fetched range is the best guess available.
            _nightWindow.value = loaded?.sleepWindow() ?: range
        }
    }

    /**
     * The next night in that direction that the watch actually staged.
     *
     * Stepping a day at a time would land on nights the watch was off the wrist
     * for, which show as an empty screen the reader has to work out is empty.
     * Nothing moves when there is no such night, so the ends of the history are
     * a button that does nothing rather than a run of blanks.
     */
    fun shiftNight(by: Int) {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch {
            val found = withContext(Dispatchers.IO) {
                var days = _nightsAgo.value
                repeat(MAX_NIGHT_SEARCH_DAYS) {
                    val next = days + by
                    // Tonight is as recent as it gets.
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
        zoom(entry.startedAtMs..(entry.endedAtMs ?: System.currentTimeMillis()))
    }

    /**
     * Forget a recorded session.
     *
     * Only the recorded ones can be forgotten: a detected activity is worked
     * out from the minute stream on every refresh, so there is nothing there
     * to delete and it would be back before the screen closed.
     */
    fun deleteActivity(entry: RecordedEntry) {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch {
            withContext(Dispatchers.IO) { runCatching { service.deleteWorkout(entry.workout.id) } }
                .onFailure { Log.w(TAG, "delete: refused", it) }
            _selectedActivity.value = null
            // The log is rebuilt on age alone, and it has just become wrong.
            _state.value = _state.value.copy(activityLogAtMs = 0)
            refresh()
        }
    }

    fun followLive() {
        _window.value = null
        refresh()
    }

    /**
     * Start and stop write set boundaries, so the chart can shade the work and
     * rest intervals rather than showing one undifferentiated line.
     */
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

    /// Poll the battery while the app is in front.
    ///
    /// The charging indicator is only as truthful as its last reading, and
    /// between the background polls a reading is recent but out of date — it
    /// would go on claiming a charger works seconds after it was pulled out.
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

    /// Deliberately send an oversize frame, to find where the watch stops
    /// taking them. Over the limit it reboots, which is the measurement.
    fun probeFrame(bytes: Int) {
        val service = WatchRepository.get() ?: return
        Log.w(TAG, "probe: sending a frame of $bytes bytes")
        viewModelScope.launch(Dispatchers.IO) {
            runCatching { service.probeFrame(bytes.toUInt()) }
                .onFailure { Log.w(TAG, "probe: refused", it) }
        }
    }

    fun requestScreens() {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch(Dispatchers.IO) { runCatching { service.requestScreens() } }
    }

    /**
     * Hand a whole list to the watch and wait to be told it took it.
     *
     * The screen and activity sets are written and then read straight back —
     * the watch may reject or reorder — so what confirms them is the watch's
     * own answer, not the write returning. Nothing is called saved until that
     * answer matches what went out.
     */
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
            // In order: the menu is listed in the order it was written, so a
            // menu that came back reordered is not the menu that was asked for.
            confirm = { it.activities().filter { a -> a.enabled }.map { a -> a.id } == ids },
            unconfirmed = "The watch did not come back with the new menu.",
        )
    }

    /**
     * The health features, as one batch.
     *
     * There is no read side for these at all — the watch cannot be asked what
     * it has on — so the only thing that can be confirmed is that every frame
     * went out. Each write carries the whole set, so they go one at a time and
     * the last one is what the watch is left holding.
     */
    fun applyFeatures(changes: List<Pair<UShort, Boolean>>) {
        save(
            send = { service -> changes.forEach { (id, on) -> service.setHealthFeature(id, on) } },
            reask = null,
            confirm = null,
            unconfirmed = "",
        )
    }

    /**
     * The wearer's profile, which the watch holds rather than this app.
     *
     * A write replaces the whole record, and the set is answered with Null, so
     * the only confirmation is reading it back — same shape as the screen and
     * activity lists.
     */
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
        /// Asks the watch again for what it now holds. Without this the wait
        /// is just a long look at whatever the last reply happened to say.
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
            // The reason rather than a guess at it: this fails for things the
            // watch never saw — a glyph size it has not declared yet — and
            // calling those a refusal sends anyone reading it looking at the
            // wrong end of the link.
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
                // What the watch came back with, so a confirmation that never
                // arrives can be told apart from one that disagreed.
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

    /** Called once the screen has acted on the outcome, win or lose. */
    fun acknowledgeSave() {
        _save.value = SaveState.Idle
    }

    fun requestDeviceConfig() {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch(Dispatchers.IO) { runCatching { service.requestDeviceConfig() } }
    }

    /**
     * Ask the watch to begin a session.
     *
     * Nothing is shown as started here: the watch stamps the session with its
     * own clock and reports it back, and that report is what the screen
     * follows. A start the watch declines therefore shows as nothing happening,
     * which is the truth.
     */
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

    /// The clock is put right on every connection; this is the same thing on
    /// demand, for when the drift is the thing being looked at.
    fun setWatchTime() {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch(Dispatchers.IO) { runCatching { syncWatchClock(service) } }
    }


    /**
     * Erase the watch and let go of it.
     *
     * The order matters and is the whole reason this is one call: forgetting
     * the key while the watch still holds it locks the app out of a watch it
     * can no longer authenticate to. So the reset goes out first, and
     * [onForgotten] — which is what clears the key here — only runs once the
     * watch has acted on it.
     *
     * The reboot is the acknowledgement. Nothing is sent back for a factory
     * reset; the link simply dies as the watch erases itself and restarts, so
     * that is what is waited for.
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

    /**
     * Phone notifications, both halves at once.
     *
     * The feature tag and the ANCS switch are two mechanisms for one thing: the
     * tag is the entitlement and the config is the live setting. Offering them
     * as two switches let them disagree, and since the tag has no read side
     * nothing could ever detect that they had — so the tag follows the switch.
     */
    fun setNotifications(enabled: Boolean) {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch(Dispatchers.IO) {
            runCatching {
                service.setNotifications(enabled)
                service.setHealthFeature(NOTIFICATION_FEATURE, enabled)
            }
        }
    }

    /**
     * A notification of our own, to exercise the path without reading the
     * phone's real ones. Posting and clearing are separate: the watch keeps it
     * on screen until told otherwise, and the id is how it is told.
     */
    fun postTestNotification() {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch(Dispatchers.IO) {
            runCatching {
                // One at a time, or the previous id is lost and the watch is
                // left holding a notification nothing can clear.
                _testNotification.value?.let { service.dismissNotification(it) }
                val id = service.postNotification(
                    // The watch caches an app's icon by this id and stops
                    // asking once it has an answer — including the empty one.
                    // A fresh id every time forces it to ask again.
                    appId = "$TEST_APP_ID.t${System.currentTimeMillis() % 100000}",
                    // Both empty so the watch shows the comparison line and
                    // nothing else; it lays the three fields out top to bottom
                    // and a title above the glyph makes it harder to size
                    // against the letters beside it.
                    title = "",
                    subtitle = "",
                    // The middle character is outside the watch's own font, so
                    // it has to ask us to draw it, with the watch's own
                    // capitals either side to size it against. It caches a
                    // glyph by codepoint just as it caches an icon by app id,
                    // so a fixed character is asked for exactly once ever;
                    // rotating through the fullwidth Latin letters keeps every
                    // tap a fresh request. They are also asymmetric, which is
                    // what showed the watch reads these bitmaps column-major.
                    message = "ABC ${probeGlyph()} ABC",
                    category = NotificationCategory.SOCIAL,
                )
                _testNotification.value = id
            }
        }
    }

    /** A fullwidth Latin letter, different each second. */
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
        /** The cap Rust reduces to; roughly one point per horizontal pixel. */
        const val MAX_CHART_POINTS = 1200u
        /** Our own package, so the watch asks us for an icon we actually have. */
        const val TEST_APP_ID = "dev.davidv.withoutings"
    }
}
