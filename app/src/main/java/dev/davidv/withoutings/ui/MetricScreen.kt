package dev.davidv.withoutings.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.ExpandMore
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Text
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.unit.dp
import dev.davidv.withoutings.ui.theme.AppTheme

/**
 * The sensor-history template.
 *
 * Reached by tapping any tile on Now, and the same skeleton for every series:
 * an accent summary card carrying the metric's meaningful aggregate and its
 * own-history delta, range chips, a chart with a personal-baseline band and
 * session attribution, three parallel stats, then an attribution list.
 *
 * It replaced a screen that showed Latest / Min / Max over a bare line — three
 * numbers that told the reader nothing they could act on.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MetricScreen(
    style: MetricStyle,
    state: UiState,
    window: LongRange,
    nowMs: Long,
    onWindowChange: (LongRange) -> Unit,
    onRange: (Long) -> Unit,
    onSelectStyle: (MetricStyle) -> Unit,
    onOpenSession: (Session) -> Unit,
    onBack: () -> Unit,
) {
    var scrubAtMs by remember { mutableStateOf<Long?>(null) }
    var switching by remember { mutableStateOf(false) }
    val sheet = rememberModalBottomSheetState()

    // The series carries a point beyond each edge so the line can leave the
    // plot; they are not part of what is being summarised.
    val visible = state.metric.filter { it.atMs in window }
    val sessions = state.activityLog
        .map { it.session(nowMs) }
        .filter { it.span.overlaps(Span(window.first, window.last)) }
    val summary = metricSummary(
        style = style,
        window = visible,
        baseline = state.metricBaseline,
        sessions = sessions,
        latest = state.latest[style],
        nowMs = nowMs,
    )

    DetailScaffold(
        title = style.label,
        subtitle = "${fullDate(window.last)} · last ${spanLabel(window.last - window.first)}",
        onBack = onBack,
        trailing = {
            GlyphButton(Icons.Rounded.ExpandMore, "Choose a series") { switching = true }
        },
    ) {
        Column(
            Modifier.weight(1f).verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(AppTheme.space.blockMetric),
        ) {
            SummaryCard(summary)

            ChipRow(
                RangeSpan.entries.map { it to it.label },
                RangeSpan.matching(window.last - window.first),
            ) { onRange(it.spanMs) }

            ChartCard {
                Text(
                    "${style.unit} · ${formatValue(style.axis.start, 0)} – " +
                        formatValue(style.axis.endInclusive, 0),
                    Modifier.fillMaxWidth().padding(bottom = 6.dp),
                    style = AppTheme.type.axisSmall,
                    color = AppTheme.colors.onSurfaceDim,
                )
                ValueChart(
                    points = state.metric,
                    window = window,
                    axis = style.axis,
                    decimals = style.decimals,
                    height = CHART_HEIGHT,
                    onWindowChange = onWindowChange,
                    scrubAtMs = scrubAtMs,
                    onScrub = { scrubAtMs = it },
                    sessions = sessions.chartSessions(),
                    labelSessions = true,
                    bands = summary.band?.let {
                        listOf(ValueBand(it.start, it.endInclusive, AppTheme.chart.bandAlpha))
                    } ?: emptyList(),
                    guides = summary.guide?.let { listOf(Guide(it)) } ?: emptyList(),
                    unit = " ${style.unit}",
                )
                Spacer(Modifier.height(6.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    if (summary.band != null) {
                        LegendSwatch(
                            MaterialTheme.colorScheme.primary
                                .copy(alpha = AppTheme.chart.legendBandAlpha),
                            "Your usual range, 14 days",
                        )
                    }
                    if (sessions.isNotEmpty()) {
                        LegendSwatch(
                            MaterialTheme.colorScheme.primary
                                .copy(alpha = AppTheme.chart.legendSessionAlpha),
                            "Recorded session",
                        )
                    }
                }
            }

            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                summary.stats.forEach { stat ->
                    StatTile(
                        stat.eyebrow,
                        stat.value,
                        stat.unit,
                        stat.footer,
                        Modifier.weight(1f),
                    )
                }
            }

            if (summary.listTitle != null) {
                SpellList(summary, style, onOpenSession)
            }
            Spacer(Modifier.height(8.dp))
        }
    }

    if (switching) {
        ModalBottomSheet(onDismissRequest = { switching = false }, sheetState = sheet) {
            SeriesPicker(style) {
                switching = false
                onSelectStyle(it)
            }
        }
    }
}

private val CHART_HEIGHT = 190.dp

/**
 * The accent card, and the only one on the screen.
 *
 * The headline is the aggregate that carries meaning across days; the
 * instantaneous value is a quiet aside because Now already owns it.
 */
@Composable
private fun SummaryCard(summary: MetricSummary) {
    AccentCard(Modifier.fillMaxWidth()) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text(
                summary.headline,
                Modifier.weight(1f),
                style = AppTheme.type.tileLabel,
                color = MaterialTheme.colorScheme.onPrimaryContainer,
            )
            Text(
                summary.aside,
                style = AppTheme.type.rowMeta,
                color = AppTheme.colors.onAccentSecondary,
            )
        }
        Spacer(Modifier.height(2.dp))
        ValueWithUnit(
            summary.value,
            summary.unit,
            AppTheme.type.focalMedium,
            color = MaterialTheme.colorScheme.onPrimaryContainer,
            unitColor = AppTheme.colors.onAccentSecondary,
        )
    }
}

/**
 * Where it went up, and what was happening at the time.
 *
 * A stretch no session accounts for is named as such rather than hidden or
 * alarmed: it is the honest answer to "what about short spikes".
 */
@Composable
private fun SpellList(
    summary: MetricSummary,
    style: MetricStyle,
    onOpenSession: (Session) -> Unit,
) {
    Column(Modifier.fillMaxWidth()) {
        Eyebrow(summary.listTitle ?: return, Modifier.padding(bottom = 4.dp))
        if (summary.spells.isEmpty()) {
            EmptyNote("Nothing in this window crossed the line.")
            return
        }
        summary.spells.sortedByDescending { it.span.fromMs }.forEachIndexed { index, spell ->
            if (index > 0) RowDivider(inset = 0.dp)
            Row(
                Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(6.dp))
                    .then(
                        spell.session?.let { session ->
                            Modifier.clickable { onOpenSession(session) }
                        } ?: Modifier
                    )
                    .padding(vertical = 7.dp, horizontal = 2.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    clock(spell.span.fromMs),
                    Modifier.width(46.dp),
                    style = AppTheme.type.rowMeta,
                    color = AppTheme.colors.onSurfaceTertiary,
                )
                Text(
                    spell.session?.name ?: "Not in a session",
                    Modifier.weight(1f),
                    style = AppTheme.type.body.copy(fontSize = AppTheme.type.sectionTitle.fontSize),
                    // Not an entity, so it is not styled like one.
                    color = if (spell.session != null) {
                        MaterialTheme.colorScheme.onSurface
                    } else {
                        AppTheme.colors.onSurfaceTertiary
                    },
                )
                Text(
                    "peak ${formatValue(spell.peak, style.decimals)}",
                    style = AppTheme.type.rowMeta,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

/**
 * Which series to look at.
 *
 * Only four fit on Now, and the rest are as real as those four; putting them
 * behind the title keeps the home screen to its four parallel tiles without
 * making the other eight unreachable.
 */
@Composable
private fun SeriesPicker(current: MetricStyle, onSelect: (MetricStyle) -> Unit) {
    Column(Modifier.padding(horizontal = AppTheme.space.screen).padding(bottom = 32.dp)) {
        Eyebrow("series", Modifier.padding(bottom = 8.dp))
        MetricStyle.entries.forEach { style ->
            Row(
                Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(AppTheme.radius.button))
                    .clickable { onSelect(style) }
                    .background(
                        if (style == current) {
                            MaterialTheme.colorScheme.primaryContainer
                        } else {
                            androidx.compose.ui.graphics.Color.Transparent
                        }
                    )
                    .padding(horizontal = 12.dp, vertical = 12.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Icon(
                    style.icon,
                    null,
                    Modifier.size(20.dp),
                    tint = if (style == current) {
                        MaterialTheme.colorScheme.onPrimaryContainer
                    } else {
                        MaterialTheme.colorScheme.onSurfaceVariant
                    },
                )
                Text(
                    style.label,
                    Modifier.weight(1f).padding(start = 12.dp),
                    style = AppTheme.type.rowTitle,
                    color = if (style == current) {
                        MaterialTheme.colorScheme.onPrimaryContainer
                    } else {
                        MaterialTheme.colorScheme.onSurface
                    },
                )
                Text(
                    style.unit,
                    style = AppTheme.type.rowMeta,
                    color = AppTheme.colors.onSurfaceTertiary,
                )
            }
        }
    }
}

/** A window's width, said the way the range chips say it. */
private fun spanLabel(ms: Long): String {
    RangeSpan.matching(ms)?.let { return it.label.lowercase() }
    val minutes = ms / 60_000
    val days = minutes / (24 * 60)
    val hours = (minutes % (24 * 60)) / 60
    val rest = minutes % 60
    return when {
        minutes < 1 -> "${ms / 1000}s"
        days > 0 -> if (hours == 0L) "${days}d" else "${days}d ${hours}h"
        hours > 0 -> if (rest == 0L) "${hours}h" else "${hours}h ${rest}m"
        else -> "${minutes}m"
    }
}
