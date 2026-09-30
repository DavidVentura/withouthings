package dev.davidv.withoutings.ui

import org.junit.Assert.assertEquals
import org.junit.Test
import uniffi.wpp_ffi.AwakeHeart
import uniffi.wpp_ffi.NightHeart
import uniffi.wpp_ffi.NightTrend
import uniffi.wpp_ffi.SessionHeart
import uniffi.wpp_ffi.Travel
import uniffi.wpp_ffi.WorkoutSummary
import uniffi.wpp_ffi.WorkoutTrend

private val DAY_ONE = dayStart(1_786_662_000_000L)

private fun session(startedAtMs: Long, minutes: Long, heart: SessionHeart?) = WorkoutTrend(
    WorkoutSummary(
        id = startedAtMs,
        startedAtMs = startedAtMs,
        endedAtMs = startedAtMs + minutes * MINUTE,
        subcategory = WEIGHTS_SUBCATEGORY,
        activity = "Weights",
        onFoot = true,
        travel = Travel.IN_PLACE,
        calories = null,
    ),
    heart,
)

class TrendOfTest {
    @Test
    fun `the trend at a reading is the median of the window up to it`() {
        val readings = listOf(160.0, 150.0, 170.0, 140.0).mapIndexed { day, value ->
            ChartPoint(DAY_ONE + day * DAY_MS, value)
        }
        val trend = trendOf(readings, 3 * DAY_MS, 1).trend.map { it.value }
        assertEquals(listOf(160.0, 155.0, 160.0, 150.0), trend)
    }

    @Test
    fun `no trend is drawn until the window holds enough readings`() {
        val readings = listOf(70.0, 150.0, 160.0, 170.0).mapIndexed { day, value ->
            ChartPoint(DAY_ONE + day * DAY_MS, value)
        }
        val trend = trendOf(readings, 28 * DAY_MS, 3).trend
        assertEquals(listOf(DAY_ONE + 2 * DAY_MS, DAY_ONE + 3 * DAY_MS), trend.map { it.atMs })
    }
}

class WeeklyTest {
    @Test
    fun `a week without a session is counted as none`() {
        val monday = weekStart(DAY_ONE + 3 * WEEK_MS)
        val window = monday..(monday + 3 * WEEK_MS - 1)
        val starts = listOf(monday + HOUR, monday + DAY_MS, monday + 2 * WEEK_MS + HOUR)
        assertEquals(listOf(2.0, 0.0, 1.0), sessionsPerWeek(starts, window).map { it.value })
    }

    @Test
    fun `weeks before the first recorded session are not counted as rest`() {
        val monday = weekStart(DAY_ONE + 8 * WEEK_MS)
        val window = (monday - 6 * WEEK_MS)..(monday + 2 * WEEK_MS - 1)
        val firstRecorded = monday - WEEK_MS + 3 * DAY_MS
        val weeks = weeklyCounts(listOf(firstRecorded, monday + HOUR), window, firstRecorded)
        assertEquals(listOf(monday to 1.0, monday + WEEK_MS to 0.0), weeks.map { it.atMs to it.value })
    }
}

class DailyTest {
    private fun day(index: Int, steps: Double, worn: Int) =
        DayTrend(DAY_ONE + index * DAY_MS, steps, null, 0.0, null, null, null, null, worn)

    @Test
    fun `a day mostly off the wrist is left out and today stays out of the trend`() {
        val days = listOf(day(0, 8000.0, 24), day(1, 1200.0, 9), day(2, 9000.0, 22), day(3, 3000.0, 11))
        val series = dailyTrend(days, DAY_ONE + 3 * DAY_MS, 7 * DAY_MS, 1) { it.steps }
        assertEquals(listOf(0, 2, 3), series.readings.map { ((it.atMs - DAY_ONE) / DAY_MS).toInt() })
        assertEquals(listOf(DAY_ONE, DAY_ONE + 2 * DAY_MS), series.trend.map { it.atMs })
    }
}

class AwakeTest {
    private fun day(index: Int, readings: UInt) =
        DayTrend(DAY_ONE + index * DAY_MS, null, null, 0.0, null, AwakeHeart(80u, readings), null, null, 12)

    @Test
    fun `a waking rate counts from the first recorded session and on enough readings`() {
        val firstSession = DAY_ONE + 2 * DAY_MS + 15 * HOUR
        assertEquals(false, awakeCounts(day(1, 100u), firstSession))
        assertEquals(true, awakeCounts(day(2, 100u), firstSession))
        assertEquals(false, awakeCounts(day(3, 20u), firstSession))
    }
}

class AxisTest {
    @Test
    fun `the axis is fitted to the readings on whole steps`() {
        val points = listOf(ChartPoint(0, 124.0), ChartPoint(1, 201.0))
        assertEquals(120.0..210.0, axisAround(points, 10.0))
    }
}

class TrendPointsTest {
    @Test
    fun `a session's figures sit on the day it started`() {
        val noon = DAY_ONE + 12 * HOUR
        val sessions = listOf(
            session(noon, 40, SessionHeart(120u, 150u, 40 * MINUTE, 0)),
            session(noon + DAY_MS, 30, null),
        )
        assertEquals(
            listOf(ChartPoint(DAY_ONE, 40.0), ChartPoint(DAY_ONE + DAY_MS, 30.0)),
            sessions.durationMinutes(),
        )
        assertEquals(listOf(ChartPoint(DAY_ONE, 150.0)), sessions.heartPoints { it.p95Bpm })
    }

    @Test
    fun `a night belongs to the morning it ended on`() {
        val night = NightTrend(
            asleepFromMs = DAY_ONE - HOUR,
            asleepToMs = DAY_ONE + 7 * HOUR,
            asleepMs = 8 * HOUR,
            score = null,
            heart = NightHeart(56u, 50u),
        )
        assertEquals(
            listOf(ChartPoint(DAY_ONE, 50.0)),
            listOf(night).nightPoints { it.heart?.restingBpm?.toDouble() },
        )
        assertEquals(emptyList<ChartPoint>(), listOf(night).nightPoints { it.score?.total?.toDouble() })
    }
}
