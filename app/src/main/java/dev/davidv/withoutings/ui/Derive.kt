package dev.davidv.withoutings.ui

import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale
import kotlin.math.abs
import kotlin.math.roundToInt

/**
 * Everything the screens say about the data, derived from the data.
 *
 * The design's rule is that a claim in the copy must be checkable against the
 * plotted series — three crossings totalling 82 minutes, a spike at 16:04 that
 * belongs to no session — so none of these strings are authored anywhere. They
 * are computed here, from the same list the chart draws, and the screens only
 * place them.
 *
 * Pure by construction: no clock, no store, no Android. Whatever needs "now"
 * takes it as an argument.
 */

/** A value at a time, whatever the series. */
data class ChartPoint(val atMs: Long, val value: Double)

/** A stretch of time the app has something to say about. */
data class Span(val fromMs: Long, val toMs: Long) {
    val durationMs: Long get() = toMs - fromMs

    fun overlaps(other: Span): Boolean = fromMs < other.toMs && other.fromMs < toMs
}

/** A recorded session, as far as attribution is concerned. */
data class Session(val span: Span, val name: String, val started: Boolean)

/** A run of samples over a threshold, and what was happening at the time. */
data class Spell(val span: Span, val peak: Double, val session: Session?)

private const val MINUTE_MS = 60_000L
const val DAY_MS = 24 * 60 * 60 * 1000L

/// A gap this long between samples over the threshold is a new spell rather
/// than a continuation. The watch samples every ten minutes at rest, so
/// anything shorter would split one climb into several.
private const val SPELL_GAP_MS = 15 * MINUTE_MS

/// Where a resting rate is read off the distribution of a day's samples.
///
/// The watch reports no resting rate of its own, so it has to come from the
/// spread: low enough down to exclude anything the person was doing, high
/// enough not to be a single stray beat.
private const val RESTING_PERCENTILE = 0.05

/**
 * The value at a percentile of the sample set, interpolated.
 *
 * Nearest-rank would make the answer jump as one sample arrives or leaves,
 * which on a screen that says "3 below your fortnight average" is the
 * difference between a claim and a flicker.
 */
fun percentile(values: List<Double>, fraction: Double): Double? {
    if (values.isEmpty()) return null
    val sorted = values.sorted()
    val position = fraction.coerceIn(0.0, 1.0) * (sorted.size - 1)
    val below = position.toInt()
    val above = (below + 1).coerceAtMost(sorted.size - 1)
    return sorted[below] + (sorted[above] - sorted[below]) * (position - below)
}

/** The resting rate implied by a window of samples. */
fun restingRate(points: List<ChartPoint>): Double? =
    percentile(points.map { it.value }, RESTING_PERCENTILE)

/**
 * The resting rate for each day the window covers, oldest first.
 *
 * Days with no samples are absent rather than zero: a day the watch was off
 * the wrist did not have a resting rate of nothing.
 */
fun restingByDay(points: List<ChartPoint>, dayStartOf: (Long) -> Long): List<Pair<Long, Double>> =
    points.groupBy { dayStartOf(it.atMs) }
        .mapNotNull { (day, samples) -> restingRate(samples)?.let { day to it } }
        .sortedBy { it.first }

/**
 * How many days back you have to go to find a resting rate as low as today's.
 *
 * Null when there is no such day in the history given, which is a different
 * claim from "none" and is why the screen says "in the last N days" instead.
 */
fun daysSinceLower(history: List<Pair<Long, Double>>, today: Double): Int? {
    val earlier = history.dropLast(1)
    val index = earlier.indexOfLast { it.second <= today }
    if (index < 0) return null
    return earlier.size - index
}

/** Runs of samples above a threshold, each attributed to a session if one covers it. */
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
        // A single sample over the line still took time; the sampling interval
        // is the shortest honest duration to give it.
        val span = if (run.size > 1) {
            Span(run.first().atMs, run.last().atMs)
        } else {
            Span(run.first().atMs, run.first().atMs + samplingIntervalMs(points))
        }
        Spell(span, run.maxOf { it.value }, attribution(span, sessions))
    }
}

/** The typical gap between samples, for turning a count of them into a duration. */
fun samplingIntervalMs(points: List<ChartPoint>): Long {
    if (points.size < 2) return MINUTE_MS
    val ordered = points.sortedBy { it.atMs }
    val gaps = ordered.zipWithNext { a, b -> b.atMs - a.atMs }.filter { it > 0 }
    if (gaps.isEmpty()) return MINUTE_MS
    return gaps.sorted()[gaps.size / 2]
}

/**
 * Which session a stretch of time belongs to.
 *
 * The session that covers most of it wins, and a stretch no session covers
 * stays unattributed rather than being handed to the nearest one — the design
 * counts those deliberately, as the honest answer to "what about short spikes".
 */
fun attribution(span: Span, sessions: List<Session>): Session? = sessions
    .map { it to overlapMs(span, it.span) }
    .filter { it.second > 0 }
    .maxByOrNull { it.second }
    ?.first

private fun overlapMs(a: Span, b: Span): Long =
    (minOf(a.toMs, b.toMs) - maxOf(a.fromMs, b.fromMs)).coerceAtLeast(0)

/** Total time over the threshold, across every spell. */
fun timeAbove(spells: List<Spell>): Long = spells.sumOf { it.span.durationMs }

/** Time over the threshold that no recorded session accounts for. */
fun unattributedTime(spells: List<Spell>): Long =
    spells.filter { it.session == null }.sumOf { it.span.durationMs }

/**
 * The band a person's readings usually sit in, from their own history.
 *
 * A fact about them, not about their calendar: an earlier design drew a
 * per-hour expected range, which assumes the same routine every day and reads
 * as wrong the moment a workout moves.
 */
fun personalBand(points: List<ChartPoint>): ClosedFloatingPointRange<Double>? {
    val low = percentile(points.map { it.value }, 0.1) ?: return null
    val high = percentile(points.map { it.value }, 0.5) ?: return null
    if (high <= low) return null
    return low..high
}

/** The mean of a series, for a window's headline average. */
fun mean(points: List<ChartPoint>): Double? =
    if (points.isEmpty()) null else points.sumOf { it.value } / points.size

// ---------------------------------------------------------------- formatting

/**
 * A whole number, run together.
 *
 * The design groups thousands with a thin space. At these sizes on a phone the
 * gap reads as two numbers rather than one, so counts are left ungrouped.
 */
fun grouped(value: Long): String = value.toString()

fun grouped(value: Double, decimals: Int): String =
    if (decimals == 0) value.roundToInt().toString()
    else String.format(Locale.US, "%.${decimals}f", value)

private val fullDateFormat = SimpleDateFormat("EEEE d MMMM", Locale.getDefault())
private val weekdayFormat = SimpleDateFormat("EEEE", Locale.getDefault())
private val dayMonthFormat = SimpleDateFormat("d MMMM", Locale.getDefault())
private val clockFormat = SimpleDateFormat("HH:mm", Locale.getDefault())
private val clockSecondsFormat = SimpleDateFormat("HH:mm:ss", Locale.getDefault())

/** "Sunday 2 August". Never "2/8/26". */
fun fullDate(atMs: Long): String = fullDateFormat.format(Date(atMs))

fun dayAndMonth(atMs: Long): String = dayMonthFormat.format(Date(atMs))

/** 24-hour, everywhere. */
fun clock(atMs: Long): String = clockFormat.format(Date(atMs))

fun clockWithSeconds(atMs: Long): String = clockSecondsFormat.format(Date(atMs))

/** Local midnight of the day an instant falls in. */
fun dayStart(atMs: Long): Long = Calendar.getInstance().apply {
    timeInMillis = atMs
    set(Calendar.HOUR_OF_DAY, 0)
    set(Calendar.MINUTE, 0)
    set(Calendar.SECOND, 0)
    set(Calendar.MILLISECOND, 0)
}.timeInMillis

/** "Today" / "Yesterday" / the weekday / the date, whichever is shortest and true. */
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

/**
 * How old a reading is.
 *
 * Relative form only for freshness, and only while it stays short. Past an
 * hour the design moves the timestamp to the front of the string rather than
 * introducing a warning icon, so this returns the clock time instead.
 */
fun freshness(atMs: Long, nowMs: Long): String {
    val delta = nowMs - atMs
    return when {
        delta < MINUTE_MS -> "just now"
        delta < 60 * MINUTE_MS -> "${delta / MINUTE_MS} min ago"
        dayStart(atMs) == dayStart(nowMs) -> clock(atMs)
        else -> "${dayName(atMs, nowMs).lowercase()} ${clock(atMs)}"
    }
}

/** "5h 59" — a duration read as a length of sleep or of a session. */
fun hoursMinutes(ms: Long): String {
    val minutes = ms / MINUTE_MS
    return "${minutes / 60}h ${"%02d".format(minutes % 60)}"
}

/** "19 min", or "1h 12" once it is long enough for that to be clumsy. */
fun compactDuration(ms: Long): String {
    val minutes = ms / MINUTE_MS
    if (minutes < 60) return "$minutes min"
    return hoursMinutes(ms)
}

/**
 * Elapsed time on a running session, counting up.
 *
 * Minutes and seconds until it passes an hour, then hours as well. A bare
 * "90:12" is read as hours and minutes by anyone who glances at it, and this is
 * the one number that gets glanced at mid-set.
 */
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

/** "Today · 13:52 · 19 min · 650 m", dropping whatever is not known. */
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

/** A signed comparison against the person's own past: "↓ 3 vs fortnight". */
fun ownHistoryDelta(today: Double, baseline: Double, unit: String): String {
    val delta = today - baseline
    val magnitude = abs(delta)
    if (magnitude < 0.5) return "level with your $unit"
    val arrow = if (delta < 0) "↓" else "↑"
    return "$arrow ${magnitude.roundToInt()} vs $unit"
}

/**
 * The heart-rate zones, as the watch's own ecosystem divides them.
 *
 * Four, not five, and the boundaries are not invented: they were recovered from
 * the official app's per-day `hrZone*` totals by bracketing which buckets are
 * non-empty against the day's peak rate. All three land on 50 / 70 / 90 % of
 * `220 - age`, and — the part that settles it — every boundary moves down by
 * that fraction of one beat on the user's birthday, which only a formula in age
 * does. See PROTOCOL_NOTES.md.
 */
enum class HeartRateZone(val label: String) {
    Light("light"),
    Moderate("moderate"),
    Intense("intense"),
    Peak("peak"),
}

/// Where each zone starts, as a fraction of the maximum. Light starts at
/// nothing: it absorbs everything below the first boundary rather than leaving
/// a band the reading can fall out of.
private val ZONE_FLOORS = listOf(0.0, 0.50, 0.70, 0.90)

/**
 * The maximum rate implied by a birth date, on the formula the official app
 * uses: `220 - age`.
 *
 * A population regression, not a measurement — worth about ±10 bpm on any one
 * person. It is used here because matching what the watch's own app shows is
 * worth more than a better formula nobody can check.
 */
fun maxHeartRate(birthSecs: Long, nowMs: Long): Int {
    val birth = Calendar.getInstance().apply { timeInMillis = birthSecs * 1000 }
    val now = Calendar.getInstance().apply { timeInMillis = nowMs }
    var age = now.get(Calendar.YEAR) - birth.get(Calendar.YEAR)
    // Not yet had this year's birthday.
    if (now.get(Calendar.DAY_OF_YEAR) < birth.get(Calendar.DAY_OF_YEAR)) age -= 1
    return 220 - age
}

/** The lowest rate in each zone, in bpm, lowest first. */
fun zoneFloors(maxRate: Int): List<Int> = ZONE_FLOORS.map { (it * maxRate).roundToInt() }

/** Which zone a rate falls in, or null when there is no maximum to divide by. */
fun zoneOf(bpm: Double, maxRate: Int?): HeartRateZone? {
    if (maxRate == null || maxRate <= 0) return null
    val floors = zoneFloors(maxRate)
    return HeartRateZone.entries.last { bpm >= floors[it.ordinal] }
}
