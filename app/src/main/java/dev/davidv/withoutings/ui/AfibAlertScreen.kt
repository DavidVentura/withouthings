package dev.davidv.withoutings.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.MonitorHeart
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import dev.davidv.withoutings.ui.theme.AppTheme
import uniffi.wpp_ffi.AfibAlert
import uniffi.wpp_ffi.AfibReading
import uniffi.wpp_ffi.BeatIntervalKind
import uniffi.wpp_ffi.PulseAnalysis

@Composable
fun AfibAlertScreen(alert: AfibAlert?, onOpenEcg: (Long) -> Unit, onBack: () -> Unit) {
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
            SummaryCard(alert.readings)
            alert.ecgAfter?.let { ecg ->
                EntityRow(
                    icon = Icons.Rounded.MonitorHeart,
                    title = "Electrocardiogram after the alert",
                    meta = ecgMeta(ecg),
                    accent = false,
                ) { onOpenEcg(ecg.id) }
            }
            val (confirming, earlier) = alert.readings.partition { it.id == alert.id }
            confirming.forEach { ReadingCard(it, confirming = true) }
            if (earlier.isNotEmpty()) {
                Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    RowDivider(inset = 0.dp)
                    Text(
                        "Readings that led to this alert",
                        style = AppTheme.type.rowMeta,
                        color = AppTheme.colors.onSurfaceTertiary,
                    )
                }
            }
            earlier.asReversed().forEach { ReadingCard(it, confirming = false) }
            Spacer(Modifier.height(8.dp))
        }
    }
}

private fun groupOf(analysis: PulseAnalysis): String = when (analysis) {
    is PulseAnalysis.Assessed -> patternLabel(analysis.rhythm.pattern)
    is PulseAnalysis.Shifted -> "Rate changed"
    else -> "Unreadable"
}

@Composable
private fun SummaryCard(readings: List<AfibReading>) {
    if (readings.isEmpty()) return
    val groups = readings.groupingBy { groupOf(it.analysis) }.eachCount()
        .entries.sortedByDescending { it.value }
    val (top, topCount) = groups.first()
    val early = readings.sumOf { reading ->
        (reading.analysis as? PulseAnalysis.Assessed)?.rhythm?.earlyBeats?.toInt() ?: 0
    }
    val merged = readings.sumOf { reading -> reading.intervals.count { it.kind == BeatIntervalKind.MERGED } }
    val asleep = readings.count { it.asleep }
    val lines = listOfNotNull(
        "$asleep of ${readings.size} readings while asleep",
        groups.joinToString(" · ") { (label, count) -> "$count ${label.lowercase()}" },
        when (early) {
            0 -> null
            1 -> "1 early beat across the readings"
            else -> "$early early beats across the readings"
        },
        when (merged) {
            0 -> null
            1 -> "1 pulse the sensor counted twice, merged back"
            else -> "$merged pulses the sensor counted twice, merged back"
        },
    )
    ChartCard(Modifier.fillMaxWidth()) {
        Column(
            Modifier.padding(horizontal = 8.dp),
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            SpeculativeEyebrow()
            Text(
                if (topCount * 2 > readings.size) "Mostly ${top.lowercase()}" else "Mixed",
                style = AppTheme.type.resultTitle,
            )
            lines.forEach {
                Text(it, style = AppTheme.type.body, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

@Composable
private fun ReadingCard(reading: AfibReading, confirming: Boolean) {
    val intervals = reading.intervals
    ChartCard(Modifier.fillMaxWidth()) {
        Column(
            Modifier.padding(horizontal = 8.dp),
            verticalArrangement = Arrangement.spacedBy(2.dp),
        ) {
            Text(
                if (confirming) "${clock(reading.measuredAtMs)} · confirmed" else clock(reading.measuredAtMs),
                style = AppTheme.type.rowTitle,
            )
            Text(
                listOfNotNull(
                    fullDate(reading.measuredAtMs),
                    if (reading.asleep) "asleep" else "awake",
                    reading.heartRate?.let { "$it bpm" },
                    "${intervals.size} beats in ${reading.seconds} s",
                ).joinToString(" · "),
                style = AppTheme.type.rowMeta,
                color = AppTheme.colors.onSurfaceTertiary,
            )
            Spacer(Modifier.height(4.dp))
            SpeculativeEyebrow()
            Text(pulseLabel(reading.analysis), style = AppTheme.type.rowTitle)
            pulseLines(reading).forEach {
                Text(it, style = AppTheme.type.body, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
        Spacer(Modifier.height(8.dp))
        Column(
            Modifier.padding(horizontal = 8.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            IntervalPlots(intervals, (reading.analysis as? PulseAnalysis.Assessed)?.rhythm)
            IntervalLegend(intervals)
        }
    }
}
