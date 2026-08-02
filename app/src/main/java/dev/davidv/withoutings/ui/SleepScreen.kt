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

private val dayMonth = SimpleDateFormat("EEE d MMM", Locale.getDefault())

/**
 * The night named by the days it spans, or by the single day when it does not
 * cross midnight. Falls back to the fetched window for a night with no staging,
 * where the middle of the window is the morning it belongs to.
 */
private fun nightLabel(night: Night, window: LongRange): String {
    val from = night.asleepFromMs
    val to = night.asleepToMs
    if (from == null || to == null) {
        return dayMonth.format(Date(window.first + (window.last - window.first) / 2))
    }
    val start = dayMonth.format(Date(from))
    val end = dayMonth.format(Date(to))
    return if (start == end) start else "$start → $end"
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

        // Which night this is. A sleep that crosses midnight belongs to both
        // dates, and saying only one of them is what makes a screen ambiguous.
        Text(
            nightLabel(night, window),
            style = MaterialTheme.typography.titleMedium,
        )

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
        val inBedMs = perStage.values.sum()

        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            night.score?.let { score ->
                Stat("Score", "${score.total}", "of 100", Modifier.weight(1f))
            }
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
                        // Of the whole night rather than of sleep, so awake has
                        // a denominator too and the four read as one split.
                        if (inBedMs > 0) "%.0f%%".format(100.0 * total / inBedMs) else "",
                        Modifier.weight(1f),
                    )
                }
            }
            // What the score is made of, so a total can be argued with rather
            // than taken on faith.
            night.score?.let { score ->
                Text(
                    listOf(
                        "Duration ${score.duration}",
                        "Efficiency ${score.efficiency}",
                        "Deep ${score.deep}",
                        "REM ${score.rem}",
                        "Continuity ${score.continuity}",
                    ).joinToString("  ·  "),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }

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
