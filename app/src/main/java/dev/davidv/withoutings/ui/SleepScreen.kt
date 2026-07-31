package dev.davidv.withoutings.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import uniffi.wpp_ffi.Night

private val hourMin = SimpleDateFormat("HH:mm", Locale.getDefault())

@Composable
fun SleepScreen(
    night: Night?,
    window: LongRange,
    onWindowChange: (LongRange) -> Unit,
    onShift: (Int) -> Unit,
    onBack: () -> Unit,
) {
    Page("Sleep", onBack) {
        if (night == null) {
            Text("No data for this night.", style = MaterialTheme.typography.bodyLarge)
            return@Page
        }

        val asleep = night.asleepFromMs?.let { from ->
            night.asleepToMs?.let { to -> from..to }
        }
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Stat(
                "Asleep",
                asleep?.let { hourMin.format(Date(it.first)) } ?: "—",
                asleep?.let { "to ${hourMin.format(Date(it.last))}" } ?: "not detected",
                Modifier.weight(1f),
            )
            Stat(
                "Duration",
                asleep?.let {
                    val minutes = (it.last - it.first) / 60_000
                    "${minutes / 60}h${"%02d".format(minutes % 60)}"
                } ?: "—",
                "",
                Modifier.weight(1f),
            )
            Stat(
                "HRV",
                night.medianRmssd?.let { "%.0f".format(it) } ?: "—",
                "ms median",
                Modifier.weight(1f),
            )
        }

        Text(
            "Sleep is inferred from heart rate, not reported by the watch. " +
                "Lying awake and still reads the same as sleeping.",
            style = MaterialTheme.typography.bodySmall,
        )

        val scheme = MaterialTheme.colorScheme
        val bands = night.charging.bands(scheme.outlineVariant.copy(alpha = 0.5f)) +
            (asleep?.let { listOf(Band(it.first, it.last, scheme.primary.copy(alpha = 0.10f))) }
                ?: emptyList())

        Text("Heart rate", style = MaterialTheme.typography.labelSmall)
        ValueChart(
            points = night.hr.map { ChartPoint(it.atMs, it.value) },
            bands = bands,
            window = window,
            onWindowChange = onWindowChange,
            axis = 30.0..120.0,
            decimals = 0,
            height = 180.dp,
            lineColor = scheme.primary,
            gridColor = scheme.outlineVariant,
            axisColor = scheme.outline,
            labelColor = scheme.onSurfaceVariant,
        )

        Text("HRV (RMSSD)", style = MaterialTheme.typography.labelSmall)
        ValueChart(
            points = night.rmssd.map { ChartPoint(it.atMs, it.value) },
            bands = bands,
            window = window,
            onWindowChange = onWindowChange,
            axis = 0.0..200.0,
            decimals = 0,
            height = 150.dp,
            lineColor = scheme.tertiary,
            gridColor = scheme.outlineVariant,
            axisColor = scheme.outline,
            labelColor = scheme.onSurfaceVariant,
        )

        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            FilterChip(
                selected = false,
                onClick = { onShift(1) },
                label = { Text("Earlier night") },
            )
            FilterChip(
                selected = false,
                onClick = { onShift(-1) },
                label = { Text("Later night") },
            )
        }
    }
}
