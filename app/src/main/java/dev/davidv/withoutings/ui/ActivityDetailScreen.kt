package dev.davidv.withoutings.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import dev.davidv.withoutings.ui.theme.AppTheme
import kotlin.math.abs

/**
 * A finished session, read.
 *
 * Both charts share one cursor: a drag on either moves the same instant on
 * both, because the question being asked of this screen is what the two series
 * were doing at the same moment.
 */
@Composable
fun ActivityDetailScreen(
    state: UiState,
    entry: ActivityEntry?,
    window: LongRange,
    nowMs: Long,
    onWindowChange: (LongRange) -> Unit,
    onBack: () -> Unit,
) {
    var scrubAtMs by remember { mutableStateOf<Long?>(null) }

    val hr = state.hr.map { ChartPoint(it.atMs, it.bpm.toDouble()) }
    val temperature = state.workoutTemp
    val sets = state.markers.workSpans(entry?.endedAtMs ?: nowMs)
    val extent = entry?.let { it.startedAtMs..(it.endedAtMs ?: nowMs) }

    DetailScaffold(
        title = entry?.name ?: "Activity",
        subtitle = entry?.let {
            "${dayName(it.startedAtMs, nowMs)} · ${clock(it.startedAtMs)}" +
                (it.endedAtMs?.let { end -> " – ${clock(end)}" } ?: " – now")
        },
        onBack = onBack,
        gap = AppTheme.space.blockMetric,
    ) {
        if (entry == null) {
            EmptyNote("No session selected.")
            return@DetailScaffold
        }

        SummaryRail(hr, temperature)

        ChartTitle("Heart rate")
        ChartCard {
            ValueChart(
                points = hr,
                window = window,
                axis = MetricStyle.HeartRate.axis,
                decimals = 0,
                height = 150.dp,
                onWindowChange = onWindowChange,
                scrubAtMs = scrubAtMs,
                onScrub = { scrubAtMs = it },
                sessions = sets.chartSessions("set"),
                limit = extent,
                unit = " bpm",
            )
            Spacer(Modifier.height(4.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                LegendSwatch(
                    MaterialTheme.colorScheme.primary.copy(alpha = AppTheme.chart.legendSessionAlpha),
                    "Set timed here",
                )
                Spacer(Modifier.weight(1f))
                Text(
                    "pinch to zoom",
                    style = AppTheme.type.axisSmall,
                    color = AppTheme.colors.onSurfaceDim,
                )
            }
        }

        ChartTitle(
            "Skin temperature",
            if (temperature.isEmpty()) {
                "not measured"
            } else {
                "${grouped(temperature.minOf { it.value }, 1)} – " +
                    "${grouped(temperature.maxOf { it.value }, 1)} °C"
            },
        )
        ChartCard {
            if (temperature.isEmpty()) {
                EmptyNote(
                    "The watch took no skin temperature during this session.",
                    Modifier.padding(vertical = 24.dp),
                )
            } else {
                ValueChart(
                    points = temperature,
                    window = window,
                    axis = MetricStyle.Temperature.axis,
                    decimals = 1,
                    height = 96.dp,
                    onWindowChange = onWindowChange,
                    scrubAtMs = scrubAtMs,
                    onScrub = { scrubAtMs = it },
                    limit = extent,
                    // The same cursor at half strength: one of the two has to
                    // read as the one being touched.
                    cursorAlpha = 0.45f,
                    unit = " °C",
                )
            }
        }

        Spacer(Modifier.weight(1f))
    }
}

/** Three figures side by side, ruled apart. The screen's only summary. */
@Composable
private fun SummaryRail(hr: List<ChartPoint>, temperature: List<ChartPoint>) {
    val ordered = temperature.sortedBy { it.atMs }
    val rise = if (ordered.size > 1) ordered.last().value - ordered.first().value else null
    Row(
        Modifier.fillMaxWidth().height(64.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        SummaryFigure(
            "peak",
            hr.maxOfOrNull { it.value }?.toInt()?.toString() ?: "—",
            "bpm",
            Modifier.weight(1f),
        )
        RailRule()
        SummaryFigure(
            "average",
            mean(hr)?.toInt()?.toString() ?: "—",
            "bpm",
            Modifier.weight(1f),
        )
        RailRule()
        SummaryFigure(
            "temp rise",
            rise?.let { (if (it >= 0) "+" else "−") + grouped(abs(it), 1) } ?: "—",
            "°C",
            Modifier.weight(1f),
        )
    }
}

@Composable
private fun SummaryFigure(eyebrow: String, value: String, unit: String, modifier: Modifier) {
    Column(modifier, horizontalAlignment = Alignment.CenterHorizontally) {
        Eyebrow(eyebrow)
        Spacer(Modifier.height(3.dp))
        ValueWithUnit(value, unit, AppTheme.type.summaryValue)
    }
}

@Composable
private fun RailRule() {
    Box(
        Modifier
            .width(1.dp)
            .fillMaxHeight()
            .padding(vertical = 4.dp)
            .background(MaterialTheme.colorScheme.surfaceVariant)
    )
}

/**
 * A chart's name, and any standing fact about it.
 *
 * Not the cursor's readout: the tooltip travels with the cursor and saying the
 * same thing twice, in two places, at two sizes, only splits the attention of
 * whoever is dragging.
 */
@Composable
fun ChartTitle(title: String, detail: String? = null) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.Bottom) {
        Text(title, Modifier.weight(1f), style = AppTheme.type.sectionTitle)
        if (detail != null) {
            Text(
                detail,
                style = AppTheme.type.rowMeta,
                color = AppTheme.colors.onSurfaceTertiary,
            )
        }
    }
}
