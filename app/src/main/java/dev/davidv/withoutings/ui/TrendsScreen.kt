package dev.davidv.withoutings.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import dev.davidv.withoutings.ui.theme.AppTheme

@Composable
fun TrendsScreen(
    trends: Trends?,
    span: TrendSpan,
    category: TrendCategory,
    onSpan: (TrendSpan) -> Unit,
    onCategory: (TrendCategory) -> Unit,
    onAddNote: (Long, String) -> Unit,
    onDeleteNote: (Long) -> Unit,
) {
    HomeScaffold(title = "Trends", subtitle = "${category.label} · last ${span.label.lowercase()}") {
        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            ChipRow(TrendCategory.entries.map { it to it.label }, category) { onCategory(it) }
            ChipRow(TrendSpan.entries.map { it to it.label }, span) { onSpan(it) }
        }
        if (trends == null) {
            EmptyNote("Reading the history.")
            return@HomeScaffold
        }

        var window by remember(trends.window) { mutableStateOf(trends.window) }
        var scrubAtMs by remember { mutableStateOf<Long?>(null) }
        val charts = TrendCharts(
            trends.window,
            window,
            scrubAtMs,
            trends.notes.map { ChartEvent(it.atMs, it.text) },
            { window = it },
            { scrubAtMs = it },
        )

        Column(
            Modifier.weight(1f).verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(AppTheme.space.blockMetric),
        ) {
            when (category) {
                TrendCategory.Weights -> WeightsTrends(trends, charts)
                TrendCategory.Sleep -> SleepTrends(trends, charts)
                TrendCategory.Daily -> DailyTrends(trends, charts)
            }
            Spacer(Modifier.height(8.dp))
            NotesList(trends.notes, onAddNote, onDeleteNote)
            Spacer(Modifier.height(8.dp))
        }
    }
}

// Every chart on the screen shares one window and one scrubber, so a day picked
// on one is the same day read off all of them.
private class TrendCharts(
    val limit: LongRange,
    val window: LongRange,
    val scrubAtMs: Long?,
    val events: List<ChartEvent>,
    val onWindowChange: (LongRange) -> Unit,
    val onScrub: (Long?) -> Unit,
) {
    // The reading the chart's cursor lands on, found the same way the chart finds
    // it: the nearest dot, or the bar the cursor is over.
    fun readout(series: TrendSeries, unit: String, decimals: Int, form: ChartForm): String? {
        val at = scrubAtMs ?: return null
        val reading = when (form) {
            is ChartForm.Bars -> series.readings.lastOrNull { at in it.atMs until it.atMs + form.widthMs }
            else -> valueAt(series.readings.filter { it.atMs in window }, at)
        } ?: return "—"
        val trend = series.trend.firstOrNull { it.atMs == reading.atMs }
        val day = if (form is ChartForm.Bars) "week of ${dayAndMonth(reading.atMs)}" else dayAndMonth(reading.atMs)
        return "${grouped(reading.value, decimals)}$unit · $day" +
            (trend?.let { " · trend ${grouped(it.value, decimals)}" } ?: "")
    }

    @Composable
    fun Chart(
        title: String,
        series: TrendSeries,
        axisStep: Double,
        decimals: Int,
        unit: String,
        form: ChartForm = ChartForm.Scatter,
        height: Dp = CHART_HEIGHT,
        namesEvents: Boolean = false,
    ) {
        val fitted = axisAround(series.readings.filter { it.atMs in limit }, axisStep)
        // A bar is read by its length, which only means anything from zero.
        val axis = if (form is ChartForm.Bars) 0.0..fitted.endInclusive else fitted
        // The axis is fitted to the readings and changes from chart to chart,
        // so its range is said whenever no reading is.
        val range = "${grouped(axis.start, 0)} – ${grouped(axis.endInclusive, 0)}$unit"
        ChartTitle(title, readout(series, unit, decimals, form) ?: range)
        ChartCard {
            ValueChart(
                points = series.readings,
                window = window,
                axis = axis,
                decimals = decimals,
                height = height,
                onWindowChange = onWindowChange,
                scrubAtMs = scrubAtMs,
                onScrub = onScrub,
                form = form,
                limit = limit,
                unit = unit,
                readout = ChartReadout.Titled,
                trend = series.trend,
                events = if (namesEvents) events else events.map { it.copy(label = null) },
            )
        }
    }
}

@Composable
private fun WeightsTrends(trends: Trends, charts: TrendCharts) {
    val sessions = trends.weights
    if (sessions.none { it.workout.startedAtMs in trends.window }) {
        EmptyNote("No weights sessions in this span.")
        return
    }
    val trendMs = TrendCategory.Weights.trendMs
    val min = TrendCategory.Weights.minReadings
    val perWeek = TrendSeries(
        weeklyCounts(sessions.map { it.workout.startedAtMs }, trends.window, trends.recordedFromMs),
        emptyList(),
    )
    val peaks = trendOf(sessions.heartPoints { it.p95Bpm }, trendMs, min)
    val medians = trendOf(sessions.heartPoints { it.medianBpm }, trendMs, min)
    val lengths = trendOf(sessions.durationMinutes(), trendMs, min)

    Legend("each session", TrendCategory.Weights.trendLabel)
    charts.Chart("Sessions per week", perWeek, 1.0, 0, "", ChartForm.Bars(WEEK_MS), BAR_HEIGHT, namesEvents = true)
    charts.Chart("Heart rate, 95th percentile", peaks, 10.0, 0, " bpm")
    charts.Chart("Heart rate, median", medians, 10.0, 0, " bpm")
    charts.Chart("Length", lengths, 10.0, 0, " min")
}

@Composable
private fun SleepTrends(trends: Trends, charts: TrendCharts) {
    val nights = trends.nights
    if (nights.none { it.asleepToMs in trends.window }) {
        EmptyNote("The watch staged no sleep in this span.")
        return
    }
    val trendMs = TrendCategory.Sleep.trendMs
    val min = TrendCategory.Sleep.minReadings
    val scores = trendOf(nights.nightPoints { it.score?.total?.toDouble() }, trendMs, min)
    val asleep = trendOf(nights.nightPoints { it.asleepMs.toDouble() / HOUR }, trendMs, min)
    val resting = trendOf(nights.nightPoints { it.heart?.restingBpm?.toDouble() }, trendMs, min)
    val sleeping = trendOf(nights.nightPoints { it.heart?.medianBpm?.toDouble() }, trendMs, min)

    Legend("each night", TrendCategory.Sleep.trendLabel)

    charts.Chart("Sleep score", scores, 10.0, 0, "", namesEvents = true)
    charts.Chart("Time asleep", asleep, 1.0, 1, " h")
    charts.Chart("Resting heart rate", resting, 5.0, 0, " bpm")
    charts.Chart("Heart rate asleep, median", sleeping, 5.0, 0, " bpm")
}

@Composable
private fun DailyTrends(trends: Trends, charts: TrendCharts) {
    val today = dayStart(trends.window.last)
    val category = TrendCategory.Daily
    fun series(pick: (DayTrend) -> Double?) =
        dailyTrend(trends.days, today, category.trendMs, category.minReadings, pick = pick)

    val steps = series { it.steps }
    if (steps.readings.none { it.atMs in trends.window }) {
        EmptyNote("No day in this span was worn long enough to count.")
        return
    }

    Legend("each day", category.trendLabel)
    charts.Chart("Steps", steps, 2000.0, 0, "", namesEvents = true)
    charts.Chart("Energy", series { it.calories }, 200.0, 0, " kcal")
    charts.Chart("Active energy", series { it.activeCalories }, 100.0, 0, " kcal")
    charts.Chart("Distance", series { day -> day.distanceMetres?.let { it / 1000 } }, 1.0, 1, " km")
    val awake = dailyTrend(
        trends.days,
        today,
        category.trendMs,
        category.minReadings,
        counts = { awakeCounts(it, trends.recordedFromMs) },
    ) { it.awake?.medianBpm?.toDouble() }
    charts.Chart("Heart rate awake, at rest", awake, 5.0, 0, " bpm")

    fun half(min: UInt, pick: (DayTrend) -> uniffi.wpp_ffi.AwakeHeart?) = dailyTrend(
        trends.days,
        today,
        category.trendMs,
        category.minReadings,
        counts = { day -> restCounts(day, trends.recordedFromMs, pick(day), min) },
    ) { day -> pick(day)?.medianBpm?.toDouble() }
    charts.Chart("Awake at rest, 08:00 – 20:00", half(DAYTIME_MIN_READINGS) { it.daytime }, 5.0, 0, " bpm")
    charts.Chart("Awake at rest, 20:00 – 08:00", half(OVERNIGHT_MIN_READINGS) { it.overnight }, 5.0, 0, " bpm")
}

@Composable
private fun Legend(reading: String, trend: String) {
    Row(horizontalArrangement = Arrangement.spacedBy(14.dp)) {
        LegendSwatch(AppTheme.colors.dataStroke.copy(alpha = AppTheme.chart.scatterAlpha), reading)
        LegendSwatch(AppTheme.colors.dataStroke, trend)
    }
}

private val CHART_HEIGHT = 120.dp
private val BAR_HEIGHT = 90.dp
