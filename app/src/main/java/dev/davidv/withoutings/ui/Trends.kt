package dev.davidv.withoutings.ui

import uniffi.wpp_ffi.AwakeHeart
import uniffi.wpp_ffi.NightTrend
import uniffi.wpp_ffi.Note
import uniffi.wpp_ffi.SessionHeart
import uniffi.wpp_ffi.WorkoutTrend
import java.util.Calendar
import kotlin.math.ceil
import kotlin.math.floor

const val WEIGHTS_SUBCATEGORY = 16

enum class TrendSpan(val label: String, val days: Int) {
    Month("MONTH", 30),
    Quarter("3 MONTHS", 90),
    Year("YEAR", 365),
}

// A median of one or two readings is those readings, and a line drawn through
// it reads a single odd session as the direction things are going.
enum class TrendCategory(
    val label: String,
    val trendMs: Long,
    val trendLabel: String,
    val minReadings: Int,
) {
    Weights("Weights", 28 * DAY_MS, "28-day median", 3),
    Sleep("Sleep", 7 * DAY_MS, "7-night median", 4),
    Daily("Daily", 7 * DAY_MS, "7-day median", 4),
}

// Sessions and nights reach back one trend window before the span, so the trend
// line starts the span already informed rather than on its first reading alone.
data class Trends(
    val span: TrendSpan,
    val window: LongRange,
    val weights: List<WorkoutTrend>,
    val nights: List<NightTrend>,
    val days: List<DayTrend>,
    val notes: List<Note>,
    // When the first session of any kind was recorded: before it, a week
    // without a session is a week nobody measured.
    val recordedFromMs: Long,
)

const val WEEK_MS = 7 * DAY_MS

data class DayTrend(
    val dayMs: Long,
    val steps: Double?,
    val calories: Double?,
    val activeCalories: Double,
    val distanceMetres: Double?,
    val awake: AwakeHeart?,
    val daytime: AwakeHeart?,
    val overnight: AwakeHeart?,
    val wornHours: Int,
)

// Every day's figure is a total, and a day the watch spent hours off the wrist
// undercounts it by whatever was done in them. Charging takes an hour or two.
const val WORN_HOURS = 20

// A median does not undercount a part-worn day the way a total does; it only
// needs enough readings to stand on. Six hours at the watch's ten-minute pace.
const val AWAKE_MIN_READINGS = 36u

// Resting means outside every session, and before the first recorded one the
// sessions the watch ran were never recorded to be left out: its half-minute
// workout readings would count as a heart at rest.
fun awakeCounts(day: DayTrend, recordedFromMs: Long): Boolean =
    restCounts(day, recordedFromMs, day.awake, AWAKE_MIN_READINGS)

// The wearer's morning dose, taken on waking at around eight, covers the day
// until about eight in the evening. Fixed clock hours rather than the watch's
// wake-up, which only exists for nights it staged.
const val DOSED_FROM_HOUR = 8
const val DOSED_TO_HOUR = 20

// Half a day holds half the readings a whole one does, and the evening side,
// mostly asleep and left out, fewer again.
const val DAYTIME_MIN_READINGS = 36u
const val OVERNIGHT_MIN_READINGS = 18u

fun atHour(dayMs: Long, hour: Int): Long = Calendar.getInstance().apply {
    timeInMillis = dayMs
    set(Calendar.HOUR_OF_DAY, hour)
}.timeInMillis

fun restCounts(day: DayTrend, recordedFromMs: Long, rest: AwakeHeart?, minReadings: UInt): Boolean =
    day.dayMs >= dayStart(recordedFromMs) && (rest?.readings ?: 0u) >= minReadings

// Today is shown as far as it has got and kept out of the trend, which would
// otherwise dip every morning.
fun dailyTrend(
    days: List<DayTrend>,
    todayMs: Long,
    trendMs: Long,
    minReadings: Int,
    counts: (DayTrend) -> Boolean = { it.wornHours >= WORN_HOURS },
    pick: (DayTrend) -> Double?,
): TrendSeries {
    val readings = days
        .filter { counts(it) || it.dayMs == todayMs }
        .mapNotNull { day -> pick(day)?.let { ChartPoint(day.dayMs, it) } }
    val trend = trendOf(readings.filter { it.atMs != todayMs }, trendMs, minReadings).trend
    return TrendSeries(readings.sortedBy { it.atMs }, trend)
}

/** A reading and the trend it sits against, one each per session or night. */
data class TrendSeries(val readings: List<ChartPoint>, val trend: List<ChartPoint>)

// Trailing rather than centred: the trend at a day says what had been happening
// up to it, which is the only thing the last day on the chart can say.
fun trendOf(readings: List<ChartPoint>, windowMs: Long, minReadings: Int): TrendSeries {
    val sorted = readings.sortedBy { it.atMs }
    val trend = sorted.mapNotNull { point ->
        val within = sorted.filter { it.atMs in (point.atMs - windowMs + 1)..point.atMs }
        if (within.size < minReadings) return@mapNotNull null
        ChartPoint(point.atMs, percentile(within.map { it.value }, 0.5)!!)
    }
    return TrendSeries(sorted, trend)
}

fun weekStart(atMs: Long): Long = Calendar.getInstance().apply {
    timeInMillis = dayStart(atMs)
    set(Calendar.DAY_OF_WEEK, firstDayOfWeek)
    if (timeInMillis > atMs) add(Calendar.WEEK_OF_YEAR, -1)
}.timeInMillis

// Every week the span covers gets a count, a week without a session included: a
// missing bar would read as a week nobody knows about rather than one of rest.
fun sessionsPerWeek(startsMs: List<Long>, window: LongRange): List<ChartPoint> {
    val counts = startsMs.groupingBy { weekStart(it) }.eachCount()
    return generateSequence(weekStart(window.first)) { weekStart(it + WEEK_MS + HOUR) }
        .takeWhile { it <= window.last }
        .map { ChartPoint(it, (counts[it] ?: 0).toDouble()) }
        .toList()
}

// Only whole weeks are counted: the week recording began in held fewer sessions
// than were done in it, so it gets no bar. The week still running gets one, as
// far as it has got.
fun weeklyCounts(startsMs: List<Long>, window: LongRange, recordedFromMs: Long): List<ChartPoint> {
    val recordingWeek = weekStart(recordedFromMs)
    val firstWhole = if (recordingWeek == recordedFromMs) recordingWeek else weekStart(recordingWeek + WEEK_MS + HOUR)
    val from = maxOf(window.first, firstWhole)
    if (from > window.last) return emptyList()
    return sessionsPerWeek(startsMs, from..window.last)
}

// Fitted to the readings so the slope a trend has is visible; a fixed range wide
// enough for anyone's heart flattens a change of five beats into nothing.
fun axisAround(points: List<ChartPoint>, step: Double): ClosedFloatingPointRange<Double> {
    val low = points.minOfOrNull { it.value } ?: return 0.0..step
    val high = points.maxOf { it.value }
    return floor(low / step) * step..maxOf(ceil(high / step) * step, floor(low / step) * step + step)
}

// Every reading is keyed by the day it fell on, so the same session sits in the
// same column across the stacked charts.
fun List<WorkoutTrend>.durationMinutes(): List<ChartPoint> = mapNotNull { trend ->
    val ended = trend.workout.endedAtMs ?: return@mapNotNull null
    ChartPoint(dayStart(trend.workout.startedAtMs), (ended - trend.workout.startedAtMs).toDouble() / MINUTE)
}

fun List<WorkoutTrend>.heartPoints(pick: (SessionHeart) -> UShort): List<ChartPoint> =
    mapNotNull { trend ->
        trend.heart?.let { ChartPoint(dayStart(trend.workout.startedAtMs), pick(it).toDouble()) }
    }

// A night belongs to the morning it ended on, which is the day it is read on.
fun List<NightTrend>.nightPoints(pick: (NightTrend) -> Double?): List<ChartPoint> =
    mapNotNull { night -> pick(night)?.let { ChartPoint(dayStart(night.asleepToMs), it) } }
