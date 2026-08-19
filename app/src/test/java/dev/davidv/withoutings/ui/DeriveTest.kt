package dev.davidv.withoutings.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.Calendar

private const val MINUTE = 60_000L

private fun series(vararg values: Pair<Long, Double>) =
    values.map { (minute, value) -> ChartPoint(minute * MINUTE, value) }

class PercentileTest {
    @Test
    fun `interpolates between neighbours`() {
        val values = listOf(10.0, 20.0, 30.0, 40.0)
        assertEquals(10.0, percentile(values, 0.0)!!, 0.001)
        assertEquals(40.0, percentile(values, 1.0)!!, 0.001)
        assertEquals(25.0, percentile(values, 0.5)!!, 0.001)
    }

    @Test
    fun `an empty series has no percentile`() {
        assertNull(percentile(emptyList(), 0.5))
    }

    @Test
    fun `resting sits near the bottom of the spread`() {
        val day = List(100) { 55.0 + it % 5 } + List(20) { 140.0 }
        val resting = restingRate(day.mapIndexed { i, v -> ChartPoint(i * MINUTE, v) })!!
        assertTrue("resting $resting should stay under 60", resting < 60)
    }
}

class SpellsTest {
    private val walk = Session(Span(20 * MINUTE, 40 * MINUTE), "Walking", started = false)

    @Test
    fun `consecutive samples over the line form one spell`() {
        val points = series(
            0L to 60.0,
            10L to 110.0,
            20L to 120.0,
            30L to 115.0,
            40L to 70.0,
        )
        val spells = spellsAbove(points) { 100.0 }
        assertEquals(1, spells.size)
        assertEquals(10 * MINUTE, spells.single().span.fromMs)
        assertEquals(30 * MINUTE, spells.single().span.toMs)
        assertEquals(120.0, spells.single().peak, 0.001)
    }

    @Test
    fun `a long gap starts a second spell`() {
        val points = series(
            0L to 110.0,
            5L to 108.0,
            90L to 130.0,
            95L to 125.0,
        )
        assertEquals(2, spellsAbove(points) { 100.0 }.size)
    }

    @Test
    fun `a lone sample over the line still has a duration`() {
        val points = series(0L to 60.0, 10L to 130.0, 20L to 60.0)
        val spell = spellsAbove(points) { 100.0 }.single()
        assertTrue("a one-sample spell must not be instantaneous", spell.span.durationMs > 0)
    }

    @Test
    fun `a spell inside a session is attributed to it`() {
        val points = series(25L to 130.0, 30L to 135.0)
        val spell = spellsAbove(points, listOf(walk)) { 100.0 }.single()
        assertEquals("Walking", spell.session?.name)
    }

    @Test
    fun `a spell outside every session stays unattributed`() {
        val points = series(200L to 130.0, 205L to 135.0)
        val spell = spellsAbove(points, listOf(walk)) { 100.0 }.single()
        assertNull(spell.session)
        assertEquals(spell.span.durationMs, unattributedTime(listOf(spell)))
    }

    @Test
    fun `the session covering most of a spell wins`() {
        val brief = Session(Span(0, 12 * MINUTE), "Weights", started = true)
        val long = Session(Span(10 * MINUTE, 60 * MINUTE), "Walking", started = false)
        val points = series(11L to 130.0, 20L to 140.0, 25L to 135.0)
        val spell = spellsAbove(points, listOf(brief, long)) { 100.0 }.single()
        assertEquals("Walking", spell.session?.name)
    }

    @Test
    fun `total time above is the sum of the spells`() {
        val points = series(0L to 110.0, 10L to 110.0, 90L to 110.0, 100L to 110.0)
        assertEquals(20 * MINUTE, timeAbove(spellsAbove(points) { 100.0 }))
    }
}

class SleepModeTest {
    private val night = SleepSpans.of(listOf(Span(60 * MINUTE, 120 * MINUTE)))

    @Test
    fun `a window is cut at the edges of the sleep it holds`() {
        val segments = night.segments(Span(0, 180 * MINUTE))
        assertEquals(
            listOf(Mode.Awake, Mode.Asleep, Mode.Awake),
            segments.map { it.mode },
        )
        assertEquals(60 * MINUTE, segments[1].span.fromMs)
        assertEquals(120 * MINUTE, segments[1].span.toMs)
    }

    @Test
    fun `a window inside one stretch of sleep is one segment`() {
        val segments = night.segments(Span(70 * MINUTE, 90 * MINUTE))
        assertEquals(listOf(Mode.Asleep), segments.map { it.mode })
        assertEquals(70 * MINUTE, segments.single().span.fromMs)
        assertEquals(90 * MINUTE, segments.single().span.toMs)
    }

    @Test
    fun `a window with no sleep in it stays whole`() {
        val segments = SleepSpans.none.segments(Span(0, 180 * MINUTE))
        assertEquals(listOf(Mode.Awake), segments.map { it.mode })
    }

    @Test
    fun `the two modes are held apart`() {
        val points = series(
            0L to 70.0,
            30L to 72.0,
            70L to 50.0,
            80L to 48.0,
            150L to 74.0,
        )
        val figures = baselines(points, night, 0.5)
        assertEquals(72.0, figures.awake!!, 0.001)
        assertEquals(49.0, figures.asleep!!, 0.001)
    }

    @Test
    fun `a mode the window never held has no figure`() {
        val figures = baselines(series(0L to 70.0), night, 0.5)
        assertEquals(70.0, figures.awake!!, 0.001)
        assertNull(figures.asleep)
    }

    @Test
    fun `a threshold read off the mode catches a rise the flat one misses`() {
        val points = series(
            0L to 36.5,
            70L to 36.6,
            80L to 36.6,
            150L to 36.5,
        )
        val asleepRise = spellsAbove(points) { point ->
            if (night.modeAt(point.atMs) == Mode.Asleep) 36.5 else 36.9
        }
        assertEquals(1, asleepRise.size)
        assertEquals(70 * MINUTE, asleepRise.single().span.fromMs)
    }
}

class TraceRunsTest {
    @Test
    fun `readings a night apart are drawn as separate runs`() {
        val points = series(
            0L to 96.0,
            10L to 97.0,
            1440L to 95.0,
            1450L to 96.0,
        )
        val runs = points.runsWithin(60 * MINUTE)
        assertEquals(listOf(2, 2), runs.map { it.size })
    }

    @Test
    fun `a run holds every reading taken within the gap`() {
        val points = series(0L to 96.0, 10L to 97.0, 20L to 98.0)
        assertEquals(1, points.runsWithin(60 * MINUTE).size)
    }
}

class DaysSinceLowerTest {
    private fun history(vararg values: Double) =
        values.mapIndexed { index, value -> index * DAY_MS to value }

    @Test
    fun `counts back to the last day at least as low`() {
        val days = history(60.0, 58.0, 52.0, 59.0, 57.0, 54.0)
        assertEquals(3, daysSinceLower(days, 54.0))
    }

    @Test
    fun `no earlier day was lower`() {
        assertNull(daysSinceLower(history(60.0, 58.0, 57.0, 50.0), 50.0))
    }
}

class TemperatureRiseTest {
    private fun warmingSession(tail: List<Double>): List<ChartPoint> {
        val climb = List(36) { 37.1 + it * 0.025 }
        return (climb + tail).mapIndexed { i, v -> ChartPoint(i * MINUTE, v) }
    }

    @Test
    fun `a cool-down at the end does not erase the rise`() {
        val rise = temperatureRise(warmingSession(listOf(37.4, 36.9)))!!
        assertEquals(0.85, rise, 0.06)
    }

    @Test
    fun `a session that only warms reads the same as one that cools at the end`() {
        val plain = temperatureRise(warmingSession(emptyList()))!!
        val cooled = temperatureRise(warmingSession(listOf(37.4, 36.9)))!!
        assertEquals(plain, cooled, 0.03)
    }

    @Test
    fun `a single hot sample does not carry the figure`() {
        val flat = List(30) { ChartPoint(it * MINUTE, 33.0) }
        val spiked = flat + ChartPoint(30 * MINUTE, 39.0)
        assertEquals(0.0, temperatureRise(spiked)!!, 0.05)
    }

    @Test
    fun `too few samples have no rise`() {
        assertNull(temperatureRise(series(0L to 33.0, 1L to 34.0, 2L to 35.0)))
    }
}

class FormattingTest {
    @Test
    fun `counts are run together, not grouped`() {
        assertEquals("7929", grouped(7929))
        assertEquals("11744", grouped(11744))
        assertEquals("650", grouped(650))
        assertEquals("1476000", grouped(1476000))
    }

    @Test
    fun `decimals round rather than truncate`() {
        assertEquals("1476.5", grouped(1476.5, 1))
        assertEquals("36.50", grouped(36.5, 2))
        assertEquals("1477", grouped(1476.6, 0))
    }

    @Test
    fun `durations read as the design writes them`() {
        assertEquals("5h 59", hoursMinutes(5 * 3600_000L + 59 * MINUTE))
        assertEquals("19 min", compactDuration(19 * MINUTE))
        assertEquals("1h 12", compactDuration(72 * MINUTE))
    }

    @Test
    fun `the stopwatch grows an hours field rather than counting past sixty`() {
        assertEquals("12:04", stopwatch(724_000L))
        assertEquals("59:59", stopwatch(3_599_000L))
        assertEquals("1:00:00", stopwatch(3_600_000L))
        assertEquals("24:00:30", stopwatch(86_430_000L))
        assertEquals("0:00", stopwatch(-5_000L))
    }

    @Test
    fun `distance switches unit at a kilometre`() {
        assertEquals("650 m", distance(650.0))
        assertEquals("3.16 km", distance(3160.0))
    }

    @Test
    fun `freshness is relative only while it is short`() {
        val now = 12 * 3600_000L
        assertEquals("just now", freshness(now - 30_000, now))
        assertEquals("1 min ago", freshness(now - MINUTE, now))
        assertEquals("59 min ago", freshness(now - 59 * MINUTE, now))
        assertTrue(
            "past an hour the timestamp takes over from the relative form",
            !freshness(now - 3 * 3600_000L, now).endsWith("ago"),
        )
    }

    @Test
    fun `a delta is stated against the person's own past`() {
        assertEquals("↓ 3 vs fortnight", ownHistoryDelta(54.0, 57.0, "fortnight"))
        assertEquals("↑ 2 vs fortnight", ownHistoryDelta(59.0, 57.0, "fortnight"))
        assertEquals("level with your fortnight", ownHistoryDelta(57.1, 57.0, "fortnight"))
    }
}

class HeartRateZoneTest {

    @Test
    fun `zone floors match the boundaries the official app used`() {
        val whileThirtyFive = maxHeartRate(644198400L, 1768435200000L)
        assertEquals(185, whileThirtyFive)
        assertEquals(listOf(0, 93, 130, 167), zoneFloors(whileThirtyFive))
    }

    @Test
    fun `the maximum drops by one on the birthday`() {
        assertEquals(185, maxHeartRate(644198400L, 1780185600000L))
        assertEquals(184, maxHeartRate(644198400L, 1780358400000L))
    }

    @Test
    fun `a rate lands in the zone its fraction of the maximum puts it in`() {
        assertEquals(HeartRateZone.Light, zoneOf(80.0, 185))
        assertEquals(HeartRateZone.Moderate, zoneOf(93.0, 185))
        assertEquals(HeartRateZone.Intense, zoneOf(130.0, 185))
        assertEquals(HeartRateZone.Peak, zoneOf(167.0, 185))
        assertNull(zoneOf(140.0, null))
    }
}

class TrimEndTest {
    private fun at(day: Int, hour: Int, minute: Int): Long =
        Calendar.getInstance().apply {
            set(2026, Calendar.MARCH, day, hour, minute, 0)
            set(Calendar.MILLISECOND, 0)
        }.timeInMillis

    @Test
    fun `the end moves back to the time picked`() {
        val session = Span(at(4, 18, 0), at(4, 22, 30))
        assertEquals(at(4, 19, 15), trimEndAtMs(session, 19, 15))
    }

    @Test
    fun `a session running past midnight trims to the day the time falls on`() {
        val session = Span(at(4, 23, 0), at(5, 6, 0))
        assertEquals(at(5, 0, 30), trimEndAtMs(session, 0, 30))
        assertEquals(at(4, 23, 40), trimEndAtMs(session, 23, 40))
    }

    @Test
    fun `a time outside the session has no end to move to`() {
        val session = Span(at(4, 18, 0), at(4, 19, 0))
        assertNull("before the start", trimEndAtMs(session, 17, 30))
        assertNull("at the start", trimEndAtMs(session, 18, 0))
        assertNull("at the end already", trimEndAtMs(session, 19, 0))
        assertNull("after the end, so on the day before", trimEndAtMs(session, 20, 0))
    }
}
