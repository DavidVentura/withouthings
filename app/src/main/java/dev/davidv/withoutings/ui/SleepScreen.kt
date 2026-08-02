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
import uniffi.wpp_ffi.SleepStage

private val hourMin = SimpleDateFormat("HH:mm", Locale.getDefault())

private fun duration(ms: Long): String {
    val minutes = ms / 60_000
    return "${minutes / 60}h${"%02d".format(minutes % 60)}"
}

/** Enough either side to see the night end, not enough to lose it in the day. */
private const val SLEEP_MARGIN_MS = 10 * 60_000L

/**
 * The window the sleep charts open on, and the furthest they may be panned.
 *
 * The night is fetched far wider than this — evening through late morning,
 * because the heart-rate detection takes its levels from the waking hours — but
 * those hours are not what the screen is for. Null for a night with no sleep
 * period at all, where the whole fetched range is all there is to show.
 */
fun Night.sleepWindow(): LongRange? {
    val from = asleepFromMs ?: return null
    val to = asleepToMs ?: return null
    return (from - SLEEP_MARGIN_MS)..(to + SLEEP_MARGIN_MS)
}

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
        // Time in each stage, which is what the watch actually measured. The
        // span between first and last is longer by however much was spent awake.
        val perStage = night.stages
            .groupBy { it.stage }
            .mapValues { (_, bands) -> bands.sumOf { it.toMs - it.fromMs } }
        val asleepMs = perStage
            .filterKeys { it != SleepStage.AWAKE }
            .values
            .sum()

        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Stat(
                "Asleep",
                asleep?.let { hourMin.format(Date(it.first)) } ?: "—",
                asleep?.let { "to ${hourMin.format(Date(it.last))}" } ?: "not detected",
                Modifier.weight(1f),
            )
            Stat(
                "Duration",
                if (night.stages.isEmpty()) "—" else duration(asleepMs),
                if (night.stages.isEmpty()) "" else "asleep",
                Modifier.weight(1f),
            )
        }

        if (night.stages.isEmpty()) {
            Text(
                "The watch reported no staging for this night.",
                style = MaterialTheme.typography.bodySmall,
            )
        } else {
            Text("Stages", style = MaterialTheme.typography.labelSmall)
            Hypnogram(stages = night.stages, window = window)
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                for (stage in listOf(
                    SleepStage.DEEP,
                    SleepStage.REM,
                    SleepStage.LIGHT,
                    SleepStage.AWAKE,
                )) {
                    val total = perStage[stage] ?: 0L
                    Stat(
                        stageName(stage),
                        duration(total),
                        if (asleepMs > 0 && stage != SleepStage.AWAKE) {
                            "%.0f%%".format(100.0 * total / asleepMs)
                        } else {
                            ""
                        },
                        Modifier.weight(1f),
                    )
                }
            }
        }

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
            limit = night.sleepWindow(),
            axis = 30.0..120.0,
            decimals = 0,
            height = 180.dp,
            gutterLeftDp = SLEEP_GUTTER,
            lineColor = scheme.primary,
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
