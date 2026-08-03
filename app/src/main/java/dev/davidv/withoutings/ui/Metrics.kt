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
    val elevatedAbove: Double? = null,
) {
    HeartRate(
        Metric.HEART_RATE, "Heart rate", "bpm", 0, Icons.Rounded.Favorite,
        50.0..150.0, 20 * MINUTE, SummaryKind.Resting, elevatedAbove = 100.0,
    ),
    Temperature(
        Metric.TEMPERATURE, "Temperature", "°C", 1, Icons.Rounded.Thermostat,
        35.0..38.5, 10 * MINUTE, SummaryKind.Baseline,
    ),
    Steps(
        Metric.STEPS, "Steps", "steps", 0, Icons.AutoMirrored.Rounded.DirectionsWalk,
        0.0..15000.0, DAY_MS, SummaryKind.DailyTotal,
    ),
    Calories(
        Metric.CALORIES, "Energy", "kcal", 0, Icons.Rounded.LocalFireDepartment,
        0.0..3000.0, DAY_MS, SummaryKind.DailyTotal,
    ),
    Respiratory(
        Metric.RESPIRATORY_RATE, "Respiratory", "br/min", 0, Icons.Rounded.Air,
        0.0..30.0, 4 * HOUR, SummaryKind.Average,
    ),
    HrvSdnn(
        Metric.HRV_SDNN, "HRV (SDNN)", "ms", 0, Icons.Rounded.MonitorHeart,
        0.0..200.0, 3 * HOUR, SummaryKind.Average,
    ),
    HrvRmssd(
        Metric.HRV_RMSSD, "HRV (RMSSD)", "ms", 0, Icons.Rounded.MonitorHeart,
        0.0..200.0, 3 * HOUR, SummaryKind.Average,
    ),
    Spo2(
        Metric.SPO2, "Blood oxygen", "%", 0, Icons.Rounded.Bloodtype,
        90.0..100.0, 3 * HOUR, SummaryKind.Average,
    ),
    Ascent(
        Metric.ASCENT, "Climbed", "m", 1, Icons.Rounded.Terrain,
        0.0..30.0, DAY_MS, SummaryKind.DailyTotal,
    ),
    Distance(
        Metric.DISTANCE, "Distance", "m", 0, Icons.Rounded.Route,
        0.0..10000.0, DAY_MS, SummaryKind.DailyTotal,
    ),
    Battery(
        Metric.BATTERY, "Battery", "%", 0, Icons.Rounded.BatteryFull,
        0.0..100.0, HOUR, SummaryKind.Latest,
    );

    val defaultSpan: Long get() = DEFAULT_SPAN

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
