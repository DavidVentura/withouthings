package dev.davidv.withoutings.ui

/**
 * Everything a history screen says, worked out from the series it plots.
 *
 * The design's requirement is that the chart and the copy must agree — three
 * crossings of 100 totalling 82 minutes, a three-minute spike at 16:04 that no
 * session accounts for — so this takes the same lists the chart is handed and
 * returns the strings. Nothing on the screen is authored.
 */
data class MetricSummary(
    /// What the headline figure is, in words: the design leads with the
    /// meaningful aggregate rather than the latest value.
    val headline: String,
    val value: String,
    val unit: String,
    /// The quiet aside: the instantaneous value, which the home screen owns.
    val aside: String,
    /// The range this person's readings usually sit in, drawn as a horizontal
    /// band. A fact about them, not about their calendar.
    val band: ClosedFloatingPointRange<Double>?,
    /// The headline figure itself, drawn as a dashed line.
    val guide: Double?,
    val spells: List<Spell>,
    val stats: List<StatFigure>,
    /// What the "where it went up" list is called for this series, or null for
    /// a series where being high is not an event.
    val listTitle: String?,
)

/** One of the three parallel stat tiles: identical format, always three. */
data class StatFigure(
    val eyebrow: String,
    val value: String,
    val unit: String,
    val footer: String,
)

/**
 * @param window what the chart is showing
 * @param baseline a fortnight of the same series, never plotted
 * @param sessions everything recorded, for attribution
 */
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

/**
 * Resting is what carries meaning across days, so it leads and the
 * instantaneous value becomes an aside.
 */
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
        band = personalBand(baseline),
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

/**
 * Temperature is read against the person's own baseline, and a deviation is a
 * deviation in either direction — the app never calls one of them a warning.
 */
private fun baselineSummary(
    style: MetricStyle,
    window: List<ChartPoint>,
    baseline: List<ChartPoint>,
    sessions: List<Session>,
    aside: String,
): MetricSummary {
    val centre = percentile(baseline.map { it.value }, 0.5)
    val band = centre?.let { (it - BASELINE_MARGIN)..(it + BASELINE_MARGIN) }
    val spells = centre?.let { spellsAbove(window, it + BASELINE_MARGIN, sessions) } ?: emptyList()
    val peak = window.maxOfOrNull { it.value }

    return MetricSummary(
        headline = "Your baseline",
        value = centre?.let { formatValue(it, style.decimals) } ?: "—",
        unit = style.unit,
        aside = aside,
        band = band,
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

/// How far from the baseline still counts as being at it.
private const val BASELINE_MARGIN = 0.3

/**
 * A running total the watch resets at local midnight.
 *
 * The comparison is against the person's own days, not a goal: the app has no
 * goal-setting, so "66% of 12 000 steps" has nothing to be a percentage of.
 */
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
        band = average?.let { it..it },
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

/** A series with no aggregate more meaningful than what it has been doing. */
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
        band = personalBand(baseline),
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

