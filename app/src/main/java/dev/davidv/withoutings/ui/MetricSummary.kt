package dev.davidv.withoutings.ui

data class MetricSummary(
    val guides: List<Guide>,
    val spells: List<Spell>,
    val stats: List<StatFigure>,
    val listTitle: String?,
)

data class StatFigure(
    val eyebrow: String,
    val value: String,
    val unit: String,
    val footer: String,
)

fun metricSummary(
    style: MetricStyle,
    window: LongRange,
    visible: List<ChartPoint>,
    series: MetricSeries,
    sessions: List<Session>,
    nowMs: Long,
): MetricSummary = when (style.summary) {
    SummaryKind.Resting -> restingSummary(style, window, visible, series.sleep, sessions)
    SummaryKind.Baseline ->
        baselineSummary(style, window, visible, series.baseline, series.sleep, sessions)

    SummaryKind.DailyTotal -> dailyTotalSummary(style, series.dailyTotals, nowMs)
    SummaryKind.Average, SummaryKind.Latest -> plainSummary(style, visible, series.baseline)
}

// The line the figures are read against follows the wearer in and out of sleep,
// which is where a body changes what it is resting at.
private fun modeGuides(values: Baselines, sleep: SleepSpans, window: LongRange): List<Guide> =
    sleep.segments(Span(window.first, window.last))
        .mapNotNull { segment -> values[segment.mode]?.let { Guide(it, within = segment.span) } }

private fun restingSummary(
    style: MetricStyle,
    window: LongRange,
    visible: List<ChartPoint>,
    sleep: SleepSpans,
    sessions: List<Session>,
): MetricSummary {
    val resting = restingRates(visible, sleep)
    val threshold = style.elevatedAbove ?: 100.0
    val spells = spellsAbove(visible, sessions) { threshold }

    return MetricSummary(
        guides = modeGuides(resting, sleep, window),
        spells = spells,
        stats = listOf(
            StatFigure(
                "resting",
                resting.awake?.let { formatValue(it, style.decimals) } ?: "—",
                style.unit,
                "awake, in this window",
            ),
            StatFigure(
                "asleep",
                resting.asleep?.let { formatValue(it, style.decimals) } ?: "—",
                style.unit,
                "asleep, in this window",
            ),
            StatFigure(
                "avg",
                mean(visible)?.let { formatValue(it, style.decimals) } ?: "—",
                style.unit,
                "in this window",
            ),
            StatFigure(
                "above ${threshold.toInt()}",
                (timeAbove(spells) / 60_000).toString(),
                "min",
                "in ${spells.size} ${if (spells.size == 1) "spell" else "spells"}",
            ),
        ),
        listTitle = "where it went up",
    )
}

private fun baselineSummary(
    style: MetricStyle,
    window: LongRange,
    visible: List<ChartPoint>,
    baseline: List<ChartPoint>,
    sleep: SleepSpans,
    sessions: List<Session>,
): MetricSummary {
    val centre = baselines(baseline, sleep, 0.5)
    val spells = spellsAbove(visible, sessions) { point ->
        centre[sleep.modeAt(point.atMs)]?.plus(BASELINE_MARGIN)
    }
    val peak = visible.maxOfOrNull { it.value }

    return MetricSummary(
        guides = modeGuides(centre, sleep, window),
        spells = spells,
        stats = listOf(
            StatFigure(
                "baseline",
                centre.awake?.let { formatValue(it, style.decimals) } ?: "—",
                style.unit,
                "awake, over the fortnight",
            ),
            StatFigure(
                "asleep",
                centre.asleep?.let { formatValue(it, style.decimals) } ?: "—",
                style.unit,
                "asleep, over the fortnight",
            ),
            StatFigure(
                "avg",
                mean(visible)?.let { formatValue(it, style.decimals) } ?: "—",
                style.unit,
                "in this window",
            ),
            StatFigure(
                "above band",
                (timeAbove(spells) / 60_000).toString(),
                "min",
                "in ${spells.size} ${if (spells.size == 1) "stretch" else "stretches"}",
            ),
            StatFigure(
                "peak",
                peak?.let { formatValue(it, style.decimals) } ?: "—",
                style.unit,
                "in this window",
            ),
        ),
        listTitle = "where it rose",
    )
}

private const val BASELINE_MARGIN = 0.3

private fun dailyTotalSummary(
    style: MetricStyle,
    dailyTotals: Map<Long, Double>,
    nowMs: Long,
): MetricSummary {
    val today = dayStart(nowMs)

    return MetricSummary(
        guides = emptyList(),
        spells = emptyList(),
        stats = listOf(
            StatFigure(
                "today",
                dailyTotals[today]?.let { grouped(it, style.decimals) } ?: "—",
                style.unit,
                "so far",
            ),
        ),
        listTitle = null,
    )
}

private fun plainSummary(
    style: MetricStyle,
    window: List<ChartPoint>,
    baseline: List<ChartPoint>,
): MetricSummary {
    val (eyebrow, value, footer) = when (style.summary) {
        SummaryKind.Latest ->
            Triple("latest", window.maxByOrNull { it.atMs }?.value, "most recent reading")
        else -> Triple("avg", mean(window), "in this window")
    }

    return MetricSummary(
        guides = listOfNotNull(mean(baseline)?.let { Guide(it) }),
        spells = emptyList(),
        stats = listOf(
            StatFigure(
                eyebrow,
                value?.let { formatValue(it, style.decimals) } ?: "—",
                style.unit,
                footer,
            ),
            StatFigure(
                "min",
                window.minOfOrNull { it.value }?.let { formatValue(it, style.decimals) } ?: "—",
                style.unit,
                "in this window",
            ),
            StatFigure(
                "max",
                window.maxOfOrNull { it.value }?.let { formatValue(it, style.decimals) } ?: "—",
                style.unit,
                "in this window",
            ),
        ),
        listTitle = null,
    )
}
