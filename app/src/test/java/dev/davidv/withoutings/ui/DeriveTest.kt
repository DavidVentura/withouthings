package dev.davidv.withoutings.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

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
        val spells = spellsAbove(points, 100.0)
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
        assertEquals(2, spellsAbove(points, 100.0).size)
    }

    @Test
    fun `a lone sample over the line still has a duration`() {
        val points = series(0L to 60.0, 10L to 130.0, 20L to 60.0)
        val spell = spellsAbove(points, 100.0).single()
        assertTrue("a one-sample spell must not be instantaneous", spell.span.durationMs > 0)
    }

    @Test
    fun `a spell inside a session is attributed to it`() {
        val points = series(25L to 130.0, 30L to 135.0)
        val spell = spellsAbove(points, 100.0, listOf(walk)).single()
        assertEquals("Walking", spell.session?.name)
    }

    @Test
    fun `a spell outside every session stays unattributed`() {
        val points = series(200L to 130.0, 205L to 135.0)
        val spell = spellsAbove(points, 100.0, listOf(walk)).single()
        assertNull(spell.session)
        assertEquals(spell.span.durationMs, unattributedTime(listOf(spell)))
    }

    @Test
    fun `the session covering most of a spell wins`() {
        val brief = Session(Span(0, 12 * MINUTE), "Weights", started = true)
        val long = Session(Span(10 * MINUTE, 60 * MINUTE), "Walking", started = false)
        val points = series(11L to 130.0, 20L to 140.0, 25L to 135.0)
        val spell = spellsAbove(points, 100.0, listOf(brief, long)).single()
        assertEquals("Walking", spell.session?.name)
    }

    @Test
    fun `total time above is the sum of the spells`() {
        val points = series(0L to 110.0, 10L to 110.0, 90L to 110.0, 100L to 110.0)
        assertEquals(20 * MINUTE, timeAbove(spellsAbove(points, 100.0)))
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
