package dev.davidv.withoutings.ui

data class MetricSummary(
    val guide: Double?,
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
    window: List<ChartPoint>,
    baseline: List<ChartPoint>,
    sessions: List<Session>,
    dailyTotals: Map<Long, Double>,
    nowMs: Long,
): MetricSummary = when (style.summary) {
    SummaryKind.Resting -> restingSummary(style, window, sessions)
    SummaryKind.Baseline -> baselineSummary(style, window, baseline, sessions)
    SummaryKind.DailyTotal -> dailyTotalSummary(style, dailyTotals, nowMs)
    SummaryKind.Average, SummaryKind.Latest -> plainSummary(style, window, baseline)
}

private fun restingSummary(
    style: MetricStyle,
    window: List<ChartPoint>,
    sessions: List<Session>,
): MetricSummary {
    val resting = restingRate(window)
    val threshold = style.elevatedAbove ?: 100.0
    val spells = spellsAbove(window, threshold, sessions)

    return MetricSummary(
        guide = resting,
        spells = spells,
        stats = listOf(
            StatFigure(
                "resting",
                resting?.let { formatValue(it, style.decimals) } ?: "—",
                style.unit,
                "in this window",
            ),
            StatFigure(
                "avg",
                mean(window)?.let { formatValue(it, style.decimals) } ?: "—",
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
    window: List<ChartPoint>,
    baseline: List<ChartPoint>,
    sessions: List<Session>,
): MetricSummary {
    val centre = percentile(baseline.map { it.value }, 0.5)
    val spells = centre?.let { spellsAbove(window, it + BASELINE_MARGIN, sessions) } ?: emptyList()
    val peak = window.maxOfOrNull { it.value }

    return MetricSummary(
        guide = centre,
        spells = spells,
        stats = listOf(
            StatFigure(
                "baseline",
                centre?.let { formatValue(it, style.decimals) } ?: "—",
                style.unit,
                "over the fortnight",
            ),
            StatFigure(
                "avg",
                mean(window)?.let { formatValue(it, style.decimals) } ?: "—",
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
    val earlier = dailyTotals.filterKeys { it != today }.values.filter { it > 0 }
    val average = earlier.average().takeIf { it.isFinite() }

    return MetricSummary(
        guide = average,
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
        guide = mean(baseline),
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
