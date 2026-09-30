package dev.davidv.withoutings.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import dev.davidv.withoutings.ui.theme.AppTheme
import uniffi.wpp_ffi.AfibAlert
import uniffi.wpp_ffi.AfibReading

// Beat intervals the chart always spans, so readings on one screen compare by
// eye: 300 ms is 200 bpm, 1500 ms is 40 bpm.
private val INTERVAL_AXIS = 300.0..1500.0

@Composable
fun AfibAlertScreen(alert: AfibAlert?, onBack: () -> Unit) {
    DetailScaffold(
        title = "Irregular rhythm alert",
        subtitle = alert?.let { "${fullDate(it.measuredAtMs)} · ${clock(it.measuredAtMs)}" },
        onBack = onBack,
        gap = AppTheme.space.blockLoose,
    ) {
        if (alert == null) {
            EmptyNote("That alert is not here.")
            return@DetailScaffold
        }

        Column(
            Modifier.weight(1f).verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(AppTheme.space.blockLoose),
        ) {
            AccentCard(Modifier.fillMaxWidth()) {
                Text(
                    confirmation(alert),
                    style = AppTheme.type.body,
                    color = MaterialTheme.colorScheme.onPrimaryContainer,
                )
            }
            alert.readings.asReversed().forEach { reading ->
                ReadingCard(reading, confirming = reading.id == alert.id)
            }
            Spacer(Modifier.height(8.dp))
        }
    }
}

private fun confirmation(alert: AfibAlert): String {
    val first = alert.readings.firstOrNull()?.measuredAtMs ?: alert.measuredAtMs
    val hours = (alert.measuredAtMs - first) / 3_600_000.0
    val counted = alert.counted.toInt()
    val span = if (counted > 1) {
        " over ${grouped(hours, 1)} h, from ${clock(first)} on ${fullDate(first)}"
    } else {
        ""
    }
    val empty = when (alert.empty.toInt()) {
        0 -> ""
        1 -> " One of them is an empty recording (0 s) and is not shown."
        else -> " ${alert.empty} of them are empty recordings (0 s) and are not shown."
    }
    return "The watch confirmed this after $counted background readings its classifier " +
        "called AFib$span. It alerts once it counts ${alert.run} within " +
        "${alert.windowHours} h; a normal reading in between starts the count again.$empty"
}

@Composable
private fun ReadingCard(reading: AfibReading, confirming: Boolean) {
    val intervals = reading.beatIntervalsMs
    val beats = intervals.runningFold(0L) { at, interval -> at + interval.toLong() }.drop(1)
    val points = intervals.zip(beats).map { (interval, at) ->
        ChartPoint(reading.measuredAtMs + at, interval.toDouble())
    }
    val end = reading.measuredAtMs + (beats.lastOrNull() ?: 0L)
    ChartCard(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(horizontal = 8.dp)) {
            Text(
                if (confirming) "${clock(reading.measuredAtMs)} · confirmed" else clock(reading.measuredAtMs),
                style = AppTheme.type.rowTitle,
            )
            Text(
                listOfNotNull(
                    fullDate(reading.measuredAtMs),
                    reading.heartRate?.let { "$it bpm" },
                    "${intervals.size} beats in ${reading.seconds} s",
                    intervals.minOrNull()?.let { low -> "${low}–${intervals.max()} ms" },
                ).joinToString(" · "),
                style = AppTheme.type.rowMeta,
                color = AppTheme.colors.onSurfaceTertiary,
            )
        }
        Spacer(Modifier.height(6.dp))
        ValueChart(
            points = points,
            window = reading.measuredAtMs..end.coerceAtLeast(reading.measuredAtMs + 1),
            axis = INTERVAL_AXIS,
            decimals = 0,
            height = 110.dp,
            showTimeAxis = false,
            unit = " ms",
        )
    }
}
