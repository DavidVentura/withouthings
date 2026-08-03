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
import androidx.compose.material.icons.rounded.Timer
import androidx.compose.ui.graphics.vector.ImageVector
import uniffi.wpp_ffi.Marker
import uniffi.wpp_ffi.Metric
import uniffi.wpp_ffi.SetEdge

/**
 * How each series is drawn, named and read.
 *
 * [headline] is what the history screen leads with, and it is not always the
 * latest value: for heart rate the meaningful figure is the resting rate,
 * because that is what carries meaning across days, while "now" is already
 * owned by the home screen.
 */
enum class MetricStyle(
    val metric: Metric,
    val label: String,
    val unit: String,
    val decimals: Int,
    val icon: ImageVector,
    /// Fixed y-axis for this series, in its own units.
    val axis: ClosedFloatingPointRange<Double>,
    /// How long a reading still describes now. Per metric because the watch
    /// measures respiratory rate barely once an hour: one horizon for all would
    /// call the slow ones stale for doing exactly what they do.
    val freshFor: Long,
    val headline: Headline,
    /// The threshold the "where it went up" list is built from, in the series'
    /// own units. Null for a series where being high is not an event.
    val elevatedAbove: Double? = null,
) {
    HeartRate(
        Metric.HEART_RATE, "Heart rate", "bpm", 0, Icons.Rounded.Favorite,
        50.0..150.0, 20 * MINUTE, Headline.Resting, elevatedAbove = 100.0,
    ),
    Temperature(
        Metric.TEMPERATURE, "Temperature", "°C", 1, Icons.Rounded.Thermostat,
        35.0..38.5, 10 * MINUTE, Headline.Baseline,
    ),
    Steps(
        Metric.STEPS, "Steps", "steps", 0, Icons.AutoMirrored.Rounded.DirectionsWalk,
        0.0..15000.0, DAY_MS, Headline.DailyTotal,
    ),
    Calories(
        Metric.CALORIES, "Energy", "kcal", 0, Icons.Rounded.LocalFireDepartment,
        0.0..3000.0, DAY_MS, Headline.DailyTotal,
    ),
    Respiratory(
        Metric.RESPIRATORY_RATE, "Respiratory", "br/min", 0, Icons.Rounded.Air,
        0.0..30.0, 4 * HOUR, Headline.Average,
    ),
    HrvSdnn(
        Metric.HRV_SDNN, "HRV (SDNN)", "ms", 0, Icons.Rounded.MonitorHeart,
        0.0..200.0, 3 * HOUR, Headline.Average,
    ),
    HrvRmssd(
        Metric.HRV_RMSSD, "HRV (RMSSD)", "ms", 0, Icons.Rounded.MonitorHeart,
        0.0..200.0, 3 * HOUR, Headline.Average,
    ),
    Spo2(
        Metric.SPO2, "Blood oxygen", "%", 0, Icons.Rounded.Bloodtype,
        90.0..100.0, 3 * HOUR, Headline.Average,
    ),
    Ascent(
        Metric.ASCENT, "Climbed", "m", 1, Icons.Rounded.Terrain,
        0.0..30.0, DAY_MS, Headline.DailyTotal,
    ),
    Distance(
        Metric.DISTANCE, "Distance", "m", 0, Icons.Rounded.Route,
        0.0..10000.0, DAY_MS, Headline.DailyTotal,
    ),
    TrackedDuration(
        Metric.TRACKED_DURATION, "Tracked", "h", 1, Icons.Rounded.Timer,
        0.0..24.0, DAY_MS, Headline.DailyTotal,
    ),
    Battery(
        Metric.BATTERY, "Battery", "%", 0, Icons.Rounded.BatteryFull,
        0.0..100.0, HOUR, Headline.Latest,
    );

    /// One window for every series, so switching between them compares the
    /// same stretch of time rather than rescaling.
    val defaultSpan: Long get() = DEFAULT_SPAN

    companion object {
        /**
         * The four the home screen shows, and none of them is a hero.
         *
         * An earlier version gave heart rate a large accent card with a
         * sparkline; it was wrong, because at any given moment there is no
         * single most important number in this app.
         */
        val HOME = listOf(HeartRate, Steps, Calories, Temperature)
    }
}

/**
 * Which aggregate a series is summarised by.
 *
 * Not a formatting choice: it decides what the screen claims. A resting rate
 * is a fact about the person, a daily total is a fact about the day, and
 * saying one where the other is meant makes the whole card wrong.
 */
enum class Headline { Resting, Baseline, DailyTotal, Average, Latest }

internal const val MINUTE = 60_000L
internal const val HOUR = 3600_000L

private const val DEFAULT_SPAN = 6 * HOUR

/** The windows a history screen offers, and the aggregation each implies. */
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

/**
 * Set boundaries as the stretches they enclose.
 *
 * An unclosed start runs to the edge of the view: the watch marks the start of
 * a set before it can possibly know where the end is.
 */
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

/** An entry from the store, as the chart and the attribution logic see it. */
fun ActivityEntry.session(nowMs: Long) = Session(
    span = Span(startedAtMs, endedAtMs ?: nowMs),
    name = name,
    // A workout is something someone started; a walk is something the watch
    // found afterwards in counts it was keeping anyway.
    started = this is RecordedEntry,
)

fun List<Session>.chartSessions(): List<ChartSession> =
    map { ChartSession(it.span, it.name.lowercase()) }

fun List<Span>.chartSessions(label: String): List<ChartSession> =
    map { ChartSession(it, label) }
