package dev.davidv.withoutings.ui

data class MetricSummary(
    val headline: String,
    val value: String,
    val unit: String,
    val aside: String,
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
    latest: ChartPoint?,
    nowMs: Long,
): MetricSummary {
    val asideText = latest?.let {
        "${formatValue(it.value, style.decimals)} ${style.unit} now · ${freshness(it.atMs, nowMs)}"
    } ?: "nothing measured yet"

    return when (style.headline) {
        Headline.Resting -> restingSummary(style, window, baseline, sessions, asideText, nowMs)
        Headline.Baseline -> baselineSummary(style, window, baseline, sessions, asideText)
        Headline.DailyTotal -> dailyTotalSummary(style, window, baseline, asideText, nowMs)
        Headline.Average, Headline.Latest ->
            plainSummary(style, window, baseline, asideText)
    }
}

private fun restingSummary(
    style: MetricStyle,
    window: List<ChartPoint>,
    baseline: List<ChartPoint>,
    sessions: List<Session>,
    aside: String,
    nowMs: Long,
): MetricSummary {
    val today = restingRate(window)
    val threshold = style.elevatedAbove ?: 100.0
    val spells = spellsAbove(window, threshold, sessions)

    return MetricSummary(
        headline = "Resting today",
        value = today?.let { formatValue(it, style.decimals) } ?: "—",
        unit = style.unit,
        aside = aside,
        guide = today,
        spells = spells,
        stats = listOf(
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
            StatFigure(
                "no session",
                (unattributedTime(spells) / 60_000).toString(),
                "min",
                "above ${threshold.toInt()}",
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
    aside: String,
): MetricSummary {
    val centre = percentile(baseline.map { it.value }, 0.5)
    val spells = centre?.let { spellsAbove(window, it + BASELINE_MARGIN, sessions) } ?: emptyList()
    val peak = window.maxOfOrNull { it.value }

    return MetricSummary(
        headline = "Your baseline",
        value = centre?.let { formatValue(it, style.decimals) } ?: "—",
        unit = style.unit,
        aside = aside,
        guide = centre,
        spells = spells,
        stats = listOf(
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
    window: List<ChartPoint>,
    baseline: List<ChartPoint>,
    aside: String,
    nowMs: Long,
): MetricSummary {
    val today = dayStart(nowMs)
    val perDay = baseline.groupBy { dayStart(it.atMs) }
        .mapValues { (_, points) -> points.maxOf { it.value } }
    val todayTotal = perDay[today]
    val earlier = perDay.filterKeys { it != today }
    val average = earlier.values.average().takeIf { it.isFinite() }
    val best = earlier.maxByOrNull { it.value }

    return MetricSummary(
        headline = "Today",
        value = todayTotal?.let { grouped(it, style.decimals) } ?: "—",
        unit = style.unit,
        aside = aside,
        guide = average,
        spells = emptyList(),
        stats = listOf(
            StatFigure(
                "average day",
                average?.let { grouped(it, 0) } ?: "—",
                style.unit,
                "over ${earlier.size} days",
            ),
            StatFigure(
                "best day",
                best?.let { grouped(it.value, 0) } ?: "—",
                style.unit,
                best?.let { dayAndMonth(it.key) } ?: "none counted",
            ),
            StatFigure(
                "days counted",
                perDay.size.toString(),
                "",
                "of the fortnight",
            ),
        ),
        listTitle = null,
    )
}

private fun plainSummary(
    style: MetricStyle,
    window: List<ChartPoint>,
    baseline: List<ChartPoint>,
    aside: String,
): MetricSummary {
    val average = mean(window)
    val fortnight = mean(baseline)
    return MetricSummary(
        headline = if (style.headline == Headline.Latest) "Latest" else "Average in this window",
        value = when (style.headline) {
            Headline.Latest -> window.maxByOrNull { it.atMs }?.value
            else -> average
        }?.let { formatValue(it, style.decimals) } ?: "—",
        unit = style.unit,
        aside = aside,
        guide = fortnight,
        spells = emptyList(),
        stats = listOf(
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
            StatFigure(
                "readings",
                window.size.toString(),
                "",
                "in this window",
            ),
        ),
        listTitle = null,
    )
}

