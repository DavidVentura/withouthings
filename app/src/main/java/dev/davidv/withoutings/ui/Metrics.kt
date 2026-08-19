package dev.davidv.withoutings.ui

import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Air
import androidx.compose.material.icons.rounded.BatteryFull
import androidx.compose.material.icons.rounded.Bloodtype
import androidx.compose.material.icons.automirrored.rounded.DirectionsWalk
import androidx.compose.material.icons.rounded.Favorite
import androidx.compose.material.icons.rounded.LocalFireDepartment
import androidx.compose.material.icons.rounded.MonitorHeart
import androidx.compose.material.icons.rounded.Route
import androidx.compose.material.icons.rounded.Terrain
import androidx.compose.material.icons.rounded.Thermostat
import androidx.compose.ui.graphics.vector.ImageVector
import uniffi.wpp_ffi.Marker
import uniffi.wpp_ffi.Metric
import uniffi.wpp_ffi.SetEdge

enum class MetricStyle(
    val metric: Metric,
    val label: String,
    val unit: String,
    val decimals: Int,
    val icon: ImageVector,
    val axis: ClosedFloatingPointRange<Double>,
    val freshFor: Long,
    val summary: SummaryKind,
    // The longest quiet stretch the trace still crosses as one line. Past it the
    // watch was not measuring, and a line drawn over the gap invents a reading
    // for every minute of it.
    val connectWithin: Long,
    // Where a day's total sits, which is not the range the counter sweeps
    // through the day: a body that did nothing still burns most of its energy.
    val dailyAxis: ClosedFloatingPointRange<Double>? = null,
    val elevatedAbove: Double? = null,
) {
    HeartRate(
        Metric.HEART_RATE, "Heart rate", "bpm", 0, Icons.Rounded.Favorite,
        50.0..150.0, 20 * MINUTE, SummaryKind.Resting, HOUR, elevatedAbove = 100.0,
    ),
    Temperature(
        Metric.TEMPERATURE, "Temperature", "°C", 1, Icons.Rounded.Thermostat,
        35.0..38.5, 10 * MINUTE, SummaryKind.Baseline, HOUR,
    ),
    Steps(
        Metric.STEPS, "Steps", "steps", 0, Icons.AutoMirrored.Rounded.DirectionsWalk,
        0.0..15000.0, DAY_MS, SummaryKind.DailyTotal, DAY_MS,
    ),
    Calories(
        Metric.CALORIES, "Energy", "kcal", 0, Icons.Rounded.LocalFireDepartment,
        0.0..2500.0, DAY_MS, SummaryKind.DailyTotal, DAY_MS,
        dailyAxis = 1000.0..2500.0,
    ),
    Respiratory(
        Metric.RESPIRATORY_RATE, "Respiratory", "br/min", 0, Icons.Rounded.Air,
        0.0..30.0, 4 * HOUR, SummaryKind.Average, HOUR,
    ),
    HrvSdnn(
        Metric.HRV_SDNN, "HRV (SDNN)", "ms", 0, Icons.Rounded.MonitorHeart,
        0.0..200.0, 3 * HOUR, SummaryKind.Average, HOUR,
    ),
    HrvRmssd(
        Metric.HRV_RMSSD, "HRV (RMSSD)", "ms", 0, Icons.Rounded.MonitorHeart,
        0.0..200.0, 3 * HOUR, SummaryKind.Average, HOUR,
    ),
    // Measured only while asleep, so the nights are islands: connecting them
    // draws a line across every waking day.
    Spo2(
        Metric.SPO2, "Blood oxygen", "%", 0, Icons.Rounded.Bloodtype,
        90.0..100.0, 3 * HOUR, SummaryKind.Average, HOUR,
    ),
    Ascent(
        Metric.ASCENT, "Climbed", "m", 1, Icons.Rounded.Terrain,
        0.0..30.0, DAY_MS, SummaryKind.DailyTotal, DAY_MS,
    ),
    Distance(
        Metric.DISTANCE, "Distance", "m", 0, Icons.Rounded.Route,
        0.0..10000.0, DAY_MS, SummaryKind.DailyTotal, DAY_MS,
    ),
    Battery(
        Metric.BATTERY, "Battery", "%", 0, Icons.Rounded.BatteryFull,
        0.0..100.0, HOUR, SummaryKind.Latest, DAY_MS,
    );

    val defaultSpan: Long get() = DEFAULT_SPAN

    // A counter that only ever climbs until midnight says nothing as a line
    // across a week; what the day came to does.
    val accumulates: Boolean get() = summary == SummaryKind.DailyTotal

    // What a resting or baseline figure is read against changes with sleep, so
    // these are the series that have to know when the wearer was asleep.
    val comparesModes: Boolean
        get() = summary == SummaryKind.Resting || summary == SummaryKind.Baseline

    fun axisFor(form: ChartForm): ClosedFloatingPointRange<Double> = when (form) {
        is ChartForm.Line -> axis
        is ChartForm.Bars -> dailyAxis ?: axis
    }

    companion object {
        val HOME = listOf(HeartRate, Steps, Calories, Temperature)
    }
}

enum class SummaryKind { Resting, Baseline, DailyTotal, Average, Latest }

internal const val MINUTE = 60_000L
internal const val HOUR = 3600_000L

private const val DEFAULT_SPAN = 6 * HOUR

enum class RangeSpan(val label: String, val spanMs: Long) {
    SixHours("6H", 6 * HOUR),
    Day("DAY", DAY_MS),
    Week("WEEK", 7 * DAY_MS),
    Month("MONTH", 30 * DAY_MS),
    Year("YEAR", 365 * DAY_MS);

    companion object {
        fun matching(spanMs: Long): RangeSpan? = entries.firstOrNull { it.spanMs == spanMs }
    }
}

fun List<Marker>.workSpans(edgeMs: Long): List<Span> {
    val out = mutableListOf<Span>()
    var open: Long? = null
    for (marker in sortedBy { it.atMs }) {
        when (marker.edge) {
            SetEdge.START -> open = marker.atMs
            SetEdge.END -> open?.let {
                out.add(Span(it, marker.atMs))
                open = null
            }
        }
    }
    open?.let { out.add(Span(it, edgeMs)) }
    return out
}

fun ActivityEntry.session(nowMs: Long) = Session(
    span = Span(startedAtMs, endedAtMs ?: nowMs),
    name = name,
    started = this is RecordedEntry,
)

fun List<Session>.chartSessions(): List<ChartSession> =
    map { ChartSession(it.span, it.name.lowercase()) }

fun List<Span>.chartSessions(label: String): List<ChartSession> =
    map { ChartSession(it, label) }
