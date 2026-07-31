package dev.davidv.withoutings.ui

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
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.time.Instant
import java.time.ZoneId
import uniffi.wpp_ffi.DstChange
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
import uniffi.wpp_ffi.WorkoutSummary

data class UiState(
    val link: LinkState = LinkState.Disconnected,
    val snapshot: Snapshot? = null,
    val hr: List<HrPoint> = emptyList(),
    val markers: List<Marker> = emptyList(),
    val workouts: List<WorkoutSummary> = emptyList(),
    val screens: List<WatchScreen> = emptyList(),
    val metric: List<ChartPoint> = emptyList(),
    val latest: Map<MetricStyle, ChartPoint> = emptyMap(),
    val wearPosition: WearPosition = WearPosition.NOT_SET,
    val activities: List<Activity> = emptyList(),
    val features: List<HealthFeature> = emptyList(),
    /// The window the heart-rate trace was actually fetched over, so the chart
    /// draws the same range the data came from.
    val hrWindow: LongRange = 0L..0L,
    val workoutTemp: List<ChartPoint> = emptyList(),
    val ecgs: List<EcgSummary> = emptyList(),
    val liveEcg: List<Double> = emptyList(),
    /// Charging periods over the metric window, shaded behind the battery.
    val charging: List<Marker> = emptyList(),
)

private const val DEFAULT_WINDOW_MS = 10 * 60 * 1000L

/// Six seconds is what a clinical strip shows on one line at 25 mm/s.
private const val INITIAL_ECG_SPAN_MS = 6_000L

/// Frequent enough that unplugging shows up before you look away.
private const val CHARGE_POLL_MS = 8_000L

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

    /// The finished workout being looked at, if one was picked from the list.
    private val _selectedWorkout = MutableStateFlow<WorkoutSummary?>(null)
    val selectedWorkout: StateFlow<WorkoutSummary?> = _selectedWorkout.asStateFlow()

    private val _night = MutableStateFlow<Night?>(null)
    val night: StateFlow<Night?> = _night.asStateFlow()

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
                        workouts = service.workouts(50u),
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
    private fun nightRange(): LongRange {
        val midnight = todayStartMs() - _nightsAgo.value * 86_400_000L
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
        }
    }

    fun shiftNight(by: Int) {
        _nightsAgo.value = (_nightsAgo.value + by).coerceAtLeast(0)
        showNight()
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

    fun showWorkout(workout: WorkoutSummary) {
        _selectedWorkout.value = workout
        zoom(workout.startedAtMs..(workout.endedAtMs ?: System.currentTimeMillis()))
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

    fun requestScreens() {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch(Dispatchers.IO) { runCatching { service.requestScreens() } }
    }

    fun applyScreens(ids: ByteArray) {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch(Dispatchers.IO) { runCatching { service.setScreens(ids) } }
    }

    fun requestDeviceConfig() {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch(Dispatchers.IO) { runCatching { service.requestDeviceConfig() } }
    }

    fun setWearPosition(position: WearPosition) {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch(Dispatchers.IO) { runCatching { service.setWearPosition(position) } }
    }

    fun setWatchTime() {
        val service = WatchRepository.get() ?: return
        val now = Instant.now()
        val rules = ZoneId.systemDefault().rules
        val next = rules.nextTransition(now)?.let {
            DstChange(it.instant.toEpochMilli(), it.offsetAfter.totalSeconds)
        }
        viewModelScope.launch(Dispatchers.IO) {
            runCatching {
                service.setTime(now.toEpochMilli(), rules.getOffset(now).totalSeconds, next)
            }
        }
    }

    fun setActivities(ids: List<UInt>) {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch(Dispatchers.IO) { runCatching { service.setActivities(ids) } }
    }

    fun setFeature(id: UShort, enabled: Boolean) {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch(Dispatchers.IO) { runCatching { service.setHealthFeature(id, enabled) } }
    }

    fun requestRefresh() {
        val service = WatchRepository.get() ?: return
        viewModelScope.launch(Dispatchers.IO) { runCatching { service.requestRefresh() } }
    }

    private companion object {
        /** The cap Rust reduces to; roughly one point per horizontal pixel. */
        const val MAX_CHART_POINTS = 1200u
    }
}
