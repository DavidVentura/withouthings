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

@Composable
private fun SummaryCard(readings: List<AfibReading>) {
    if (readings.isEmpty()) return
    val meanings = readings.map { pulseVerdict(it.analysis).meaning }
    val counts = Meaning.entries.associateWith { meaning -> meanings.count { it == meaning } }
    val irregular = counts.getValue(Meaning.IRREGULAR)
    val headline = when {
        irregular == 1 -> "1 irregular reading"
        irregular > 1 -> "$irregular irregular readings"
        counts.getValue(Meaning.NORMAL) * 2 > readings.size -> "Looks normal"
        else -> "No irregular readings"
    }
    ChartCard(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(horizontal = 8.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Text("Local analysis", style = AppTheme.type.rowMeta, color = AppTheme.colors.onSurfaceTertiary)
            Text(headline, style = AppTheme.type.resultTitle)
            Text(
                listOf("${readings.count { it.asleep }} of ${readings.size} asleep")
                    .plus(counts.filterValues { it > 0 }.map { (meaning, count) -> "$count ${meaning.label.lowercase()}" })
                    .joinToString(" · "),
                style = AppTheme.type.body,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

@Composable
private fun ReadingCard(reading: AfibReading, confirming: Boolean) {
    val verdict = pulseVerdict(reading.analysis)
    ChartCard(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(horizontal = 8.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Text(
                listOfNotNull(
                    clock(reading.measuredAtMs),
                    "confirmed".takeIf { confirming },
                    if (reading.asleep) "asleep" else "awake",
                    reading.heartRate?.let { "$it bpm" },
                ).joinToString(" · "),
                style = AppTheme.type.rowMeta,
                color = AppTheme.colors.onSurfaceTertiary,
            )
            VerdictText(verdict)
        }
        if (verdict.meaning != Meaning.NOT_USABLE) {
            Spacer(Modifier.height(8.dp))
            Column(Modifier.padding(horizontal = 8.dp)) {
                IntervalPlots(reading.intervalsMs, (reading.analysis as? PulseAnalysis.Assessed)?.rhythm)
            }
        }
    }
}
