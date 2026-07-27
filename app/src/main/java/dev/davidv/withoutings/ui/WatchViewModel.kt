package dev.davidv.withoutings.ui

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import dev.davidv.withoutings.LinkState
import dev.davidv.withoutings.WatchRepository
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import uniffi.wpp_ffi.HrPoint
import uniffi.wpp_ffi.Marker
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
)

/** Seconds of workout history the chart shows before the user zooms. */
private const val DEFAULT_WINDOW_MS = 10 * 60 * 1000L

class WatchViewModel : ViewModel() {

    private val _state = MutableStateFlow(UiState())
    val state: StateFlow<UiState> = _state.asStateFlow()

    /** Chart viewport; null means "follow the live edge". */
    private val _window = MutableStateFlow<LongRange?>(null)
    val window: StateFlow<LongRange?> = _window.asStateFlow()

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
                if (_state.value.snapshot?.activeWorkout != null) refresh()
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
                    val range = _window.value ?: run {
                        val from = active?.startedAtMs ?: (now - DEFAULT_WINDOW_MS)
                        from..now
                    }
                    UiState(
                        link = WatchRepository.link.value,
                        snapshot = snapshot,
                        hr = service.hrSeries(range.first, range.last, MAX_CHART_POINTS),
                        markers = service.markers(range.first, range.last),
                        workouts = service.workouts(50u),
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

    /// Defaults to the last day, or the whole series if it is shorter.
    private fun metricRange(): LongRange {
        _metricWindow.value?.let { return it }
        val now = System.currentTimeMillis()
        return (now - _metricStyle.value.defaultSpan)..now
    }

    /// Opening a series frames it on its own default window: a daily total
    /// needs weeks, a 1 Hz series needs hours.
    fun showMetric(style: MetricStyle) {
        _metricStyle.value = style
        _metricWindow.value = null
        refresh()
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
        _window.value = range
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
