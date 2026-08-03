package dev.davidv.withoutings.ui

import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale
import kotlin.math.abs
import kotlin.math.roundToInt

data class ChartPoint(val atMs: Long, val value: Double)

data class Span(val fromMs: Long, val toMs: Long) {
    val durationMs: Long get() = toMs - fromMs

    fun overlaps(other: Span): Boolean = fromMs < other.toMs && other.fromMs < toMs
}

data class Session(val span: Span, val name: String, val started: Boolean)

data class Spell(val span: Span, val peak: Double, val session: Session?)

private const val MINUTE_MS = 60_000L
const val DAY_MS = 24 * 60 * 60 * 1000L

private const val SPELL_GAP_MS = 15 * MINUTE_MS

private const val RESTING_PERCENTILE = 0.05

fun percentile(values: List<Double>, fraction: Double): Double? {
    if (values.isEmpty()) return null
    val sorted = values.sorted()
    val position = fraction.coerceIn(0.0, 1.0) * (sorted.size - 1)
    val below = position.toInt()
    val above = (below + 1).coerceAtMost(sorted.size - 1)
    return sorted[below] + (sorted[above] - sorted[below]) * (position - below)
}

fun restingRate(points: List<ChartPoint>): Double? =
    percentile(points.map { it.value }, RESTING_PERCENTILE)

fun restingByDay(points: List<ChartPoint>, dayStartOf: (Long) -> Long): List<Pair<Long, Double>> =
    points.groupBy { dayStartOf(it.atMs) }
        .mapNotNull { (day, samples) -> restingRate(samples)?.let { day to it } }
        .sortedBy { it.first }

fun daysSinceLower(history: List<Pair<Long, Double>>, today: Double): Int? {
    val earlier = history.dropLast(1)
    val index = earlier.indexOfLast { it.second <= today }
    if (index < 0) return null
    return earlier.size - index
}

fun spellsAbove(
    points: List<ChartPoint>,
    threshold: Double,
    sessions: List<Session> = emptyList(),
    gapMs: Long = SPELL_GAP_MS,
): List<Spell> {
    val over = points.filter { it.value > threshold }.sortedBy { it.atMs }
    if (over.isEmpty()) return emptyList()

    val runs = mutableListOf<MutableList<ChartPoint>>()
    for (point in over) {
        val current = runs.lastOrNull()
        if (current != null && point.atMs - current.last().atMs <= gapMs) {
            current.add(point)
        } else {
            runs.add(mutableListOf(point))
        }
    }

    return runs.map { run ->
        val span = if (run.size > 1) {
            Span(run.first().atMs, run.last().atMs)
        } else {
            Span(run.first().atMs, run.first().atMs + samplingIntervalMs(points))
        }
        Spell(span, run.maxOf { it.value }, attribution(span, sessions))
    }
}

fun samplingIntervalMs(points: List<ChartPoint>): Long {
    if (points.size < 2) return MINUTE_MS
    val ordered = points.sortedBy { it.atMs }
    val gaps = ordered.zipWithNext { a, b -> b.atMs - a.atMs }.filter { it > 0 }
    if (gaps.isEmpty()) return MINUTE_MS
    return gaps.sorted()[gaps.size / 2]
}

fun attribution(span: Span, sessions: List<Session>): Session? = sessions
    .map { it to overlapMs(span, it.span) }
    .filter { it.second > 0 }
    .maxByOrNull { it.second }
    ?.first

private fun overlapMs(a: Span, b: Span): Long =
    (minOf(a.toMs, b.toMs) - maxOf(a.fromMs, b.fromMs)).coerceAtLeast(0)

fun timeAbove(spells: List<Spell>): Long = spells.sumOf { it.span.durationMs }

fun unattributedTime(spells: List<Spell>): Long =
    spells.filter { it.session == null }.sumOf { it.span.durationMs }

fun mean(points: List<ChartPoint>): Double? =
    if (points.isEmpty()) null else points.sumOf { it.value } / points.size

fun grouped(value: Long): String = value.toString()

fun grouped(value: Double, decimals: Int): String =
    if (decimals == 0) value.roundToInt().toString()
    else String.format(Locale.US, "%.${decimals}f", value)

private val fullDateFormat = SimpleDateFormat("EEEE d MMMM", Locale.getDefault())
private val weekdayFormat = SimpleDateFormat("EEEE", Locale.getDefault())
private val dayMonthFormat = SimpleDateFormat("d MMMM", Locale.getDefault())
private val clockFormat = SimpleDateFormat("HH:mm", Locale.getDefault())
private val clockSecondsFormat = SimpleDateFormat("HH:mm:ss", Locale.getDefault())

fun fullDate(atMs: Long): String = fullDateFormat.format(Date(atMs))

fun dayAndMonth(atMs: Long): String = dayMonthFormat.format(Date(atMs))

fun clock(atMs: Long): String = clockFormat.format(Date(atMs))

fun clockWithSeconds(atMs: Long): String = clockSecondsFormat.format(Date(atMs))

fun dayStart(atMs: Long): Long = Calendar.getInstance().apply {
    timeInMillis = atMs
    set(Calendar.HOUR_OF_DAY, 0)
    set(Calendar.MINUTE, 0)
    set(Calendar.SECOND, 0)
    set(Calendar.MILLISECOND, 0)
}.timeInMillis

fun dayName(atMs: Long, nowMs: Long): String {
    val today = dayStart(nowMs)
    val day = dayStart(atMs)
    return when {
        day == today -> "Today"
        day == today - DAY_MS -> "Yesterday"
        day > today - 7 * DAY_MS -> weekdayFormat.format(Date(atMs))
        else -> dayAndMonth(atMs)
    }
}

fun freshness(atMs: Long, nowMs: Long): String {
    val delta = nowMs - atMs
    return when {
        delta < MINUTE_MS -> "just now"
        delta < 60 * MINUTE_MS -> "${delta / MINUTE_MS} min ago"
        dayStart(atMs) == dayStart(nowMs) -> clock(atMs)
        else -> "${dayName(atMs, nowMs).lowercase()} ${clock(atMs)}"
    }
}

fun hoursMinutes(ms: Long): String {
    val minutes = ms / MINUTE_MS
    return "${minutes / 60}h ${"%02d".format(minutes % 60)}"
}

fun compactDuration(ms: Long): String {
    val minutes = ms / MINUTE_MS
    if (minutes < 60) return "$minutes min"
    return hoursMinutes(ms)
}

fun stopwatch(ms: Long): String {
    val seconds = (ms / 1000).coerceAtLeast(0)
    if (seconds < 3600) return String.format(Locale.US, "%d:%02d", seconds / 60, seconds % 60)
    return String.format(
        Locale.US,
        "%d:%02d:%02d",
        seconds / 3600,
        (seconds % 3600) / 60,
        seconds % 60,
    )
}

fun distance(metres: Double): String = if (metres >= 1000) {
    String.format(Locale.US, "%.2f km", metres / 1000)
} else {
    "${metres.roundToInt()} m"
}

fun sessionMeta(
    startedAtMs: Long,
    endedAtMs: Long?,
    nowMs: Long,
    withDay: Boolean = true,
    extra: List<String> = emptyList(),
): String = buildList {
    if (withDay) add(dayName(startedAtMs, nowMs))
    add(clock(startedAtMs))
    if (endedAtMs != null) add(compactDuration(endedAtMs - startedAtMs))
    addAll(extra)
}.joinToString(" · ")

fun ownHistoryDelta(today: Double, baseline: Double, unit: String): String {
    val delta = today - baseline
    val magnitude = abs(delta)
    if (magnitude < 0.5) return "level with your $unit"
    val arrow = if (delta < 0) "↓" else "↑"
    return "$arrow ${magnitude.roundToInt()} vs $unit"
}

enum class HeartRateZone(val label: String) {
    Light("light"),
    Moderate("moderate"),
    Intense("intense"),
    Peak("peak"),
}

private val ZONE_FLOORS = listOf(0.0, 0.50, 0.70, 0.90)

fun maxHeartRate(birthSecs: Long, nowMs: Long): Int {
    val birth = Calendar.getInstance().apply { timeInMillis = birthSecs * 1000 }
    val now = Calendar.getInstance().apply { timeInMillis = nowMs }
    var age = now.get(Calendar.YEAR) - birth.get(Calendar.YEAR)
    if (now.get(Calendar.DAY_OF_YEAR) < birth.get(Calendar.DAY_OF_YEAR)) age -= 1
    return 220 - age
}

fun zoneFloors(maxRate: Int): List<Int> = ZONE_FLOORS.map { (it * maxRate).roundToInt() }

fun zoneOf(bpm: Double, maxRate: Int?): HeartRateZone? {
    if (maxRate == null || maxRate <= 0) return null
    val floors = zoneFloors(maxRate)
    return HeartRateZone.entries.last { bpm >= floors[it.ordinal] }
}
