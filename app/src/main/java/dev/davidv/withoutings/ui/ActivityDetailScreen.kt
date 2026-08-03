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
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.DeleteOutline
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
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
import uniffi.wpp_ffi.ActivityTotals

@Composable
fun ActivityDetailScreen(
    state: UiState,
    entry: ActivityEntry?,
    window: LongRange,
    nowMs: Long,
    totals: ActivityTotals?,
    onWindowChange: (LongRange) -> Unit,
    onDelete: (RecordedEntry) -> Unit,
    onBack: () -> Unit,
) {
    var scrubAtMs by remember { mutableStateOf<Long?>(null) }
    var asking by remember { mutableStateOf(false) }

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
        trailing = {
            if (entry is RecordedEntry) {
                GlyphButton(Icons.Rounded.DeleteOutline, "Delete this session") { asking = true }
            }
        },
    ) {
        if (entry == null) {
            EmptyNote("No session selected.")
            return@DetailScaffold
        }

        SummaryRail(hr, temperature)

        if (totals != null && totals.steps > 0) {
            RowDivider(inset = 0.dp)
            StepRail(totals)
        }

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
                    "pinch to zoom · drag axis to pan",
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
                    cursorAlpha = 0.45f,
                    unit = " °C",
                )
            }
        }
    }

    if (!asking || entry !is RecordedEntry) return
    AlertDialog(
        onDismissRequest = { asking = false },
        containerColor = MaterialTheme.colorScheme.surfaceContainer,
        title = { Text("Delete this session?") },
        text = { Text("Set timings and workout start/end cannot be recovered") },
        confirmButton = {
            TextButton(onClick = { asking = false; onDelete(entry) }) { Text("Delete") }
        },
        dismissButton = {
            TextButton(onClick = { asking = false }) { Text("Cancel") }
        },
    )
}

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
private fun StepRail(totals: ActivityTotals) {
    Row(
        Modifier.fillMaxWidth().height(64.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        SummaryFigure("steps", grouped(totals.steps), "", Modifier.weight(1f))
        RailRule()
        SummaryFigure("climbed", grouped(totals.ascentMetres, 0), "m", Modifier.weight(1f))
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
