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
import androidx.compose.material.icons.rounded.ContentCut
import androidx.compose.material.icons.rounded.DeleteOutline
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TimeInput
import androidx.compose.material3.rememberTimePickerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import dev.davidv.withoutings.ui.theme.AppTheme
import java.util.Calendar
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
    onTrim: (RecordedEntry, Long) -> Unit,
    onBack: () -> Unit,
) {
    var scrubAtMs by remember { mutableStateOf<Long?>(null) }
    var asking by remember { mutableStateOf(false) }
    var trimming by remember { mutableStateOf(false) }

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
                if (entry.endedAtMs != null) {
                    GlyphButton(Icons.Rounded.ContentCut, "Trim the end") { trimming = true }
                }
                GlyphButton(Icons.Rounded.DeleteOutline, "Delete this session") { asking = true }
            }
        },
    ) {
        if (entry == null) {
            EmptyNote("No session selected.")
            return@DetailScaffold
        }

        FigureRail(summaryFigures(hr, temperature))

        val effort = effortFigures(totals, entry.calories)
        if (effort.isNotEmpty()) {
            RowDivider(inset = 0.dp)
            FigureRail(effort)
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

    if (entry !is RecordedEntry) return

    val session = entry.endedAtMs?.let { Span(entry.startedAtMs, it) }
    if (trimming && session != null) {
        TrimEndDialog(
            span = session,
            seedAtMs = scrubAtMs?.takeIf { it > session.fromMs && it < session.toMs }
                ?: session.toMs,
            onDismiss = { trimming = false },
            onTrim = { trimming = false; onTrim(entry, it) },
        )
    }

    if (!asking) return
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

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun TrimEndDialog(
    span: Span,
    seedAtMs: Long,
    onDismiss: () -> Unit,
    onTrim: (Long) -> Unit,
) {
    val seed = remember(seedAtMs) { Calendar.getInstance().apply { timeInMillis = seedAtMs } }
    val picker = rememberTimePickerState(
        initialHour = seed.get(Calendar.HOUR_OF_DAY),
        initialMinute = seed.get(Calendar.MINUTE),
        is24Hour = true,
    )
    val chosen = trimEndAtMs(span, picker.hour, picker.minute)

    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = MaterialTheme.colorScheme.surfaceContainer,
        title = { Text("Trim the end") },
        text = {
            Column {
                TimeInput(picker)
                Text(
                    chosen?.let {
                        "Ends ${clock(it)}, ${compactDuration(it - span.fromMs)} of session, " +
                            "${compactDuration(span.toMs - it)} dropped."
                    } ?: "Pick a time between ${clock(span.fromMs)} and ${clock(span.toMs)}.",
                    style = AppTheme.type.rowMeta,
                    color = AppTheme.colors.onSurfaceTertiary,
                )
                Text(
                    "Sets timed after the new end go with it.",
                    Modifier.padding(top = 4.dp),
                    style = AppTheme.type.rowMeta,
                    color = AppTheme.colors.onSurfaceTertiary,
                )
            }
        },
        confirmButton = {
            TextButton(enabled = chosen != null, onClick = { chosen?.let(onTrim) }) { Text("Trim") }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) { Text("Cancel") }
        },
    )
}

private data class Figure(val eyebrow: String, val value: String, val unit: String)

private fun summaryFigures(hr: List<ChartPoint>, temperature: List<ChartPoint>): List<Figure> {
    val rise = temperatureRise(temperature)
    return listOf(
        Figure("peak", hr.maxOfOrNull { it.value }?.toInt()?.toString() ?: "—", "bpm"),
        Figure("average", mean(hr)?.toInt()?.toString() ?: "—", "bpm"),
        Figure(
            "temp rise",
            rise?.let { (if (it >= 0) "+" else "−") + grouped(abs(it), 1) } ?: "—",
            "°C",
        ),
    )
}

private fun effortFigures(totals: ActivityTotals?, calories: Double?): List<Figure> = buildList {
    if (totals != null && totals.steps > 0) {
        add(Figure("steps", grouped(totals.steps), ""))
        add(Figure("climbed", grouped(totals.ascentMetres, 0), "m"))
    }
    if (calories != null) add(Figure("energy", grouped(calories, 0), "kcal"))
}

@Composable
private fun FigureRail(figures: List<Figure>) {
    Row(
        Modifier.fillMaxWidth().height(64.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        figures.forEachIndexed { index, figure ->
            if (index > 0) RailRule()
            SummaryFigure(figure.eyebrow, figure.value, figure.unit, Modifier.weight(1f))
        }
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
