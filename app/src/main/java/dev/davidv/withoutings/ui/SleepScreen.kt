package dev.davidv.withoutings.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import dev.davidv.withoutings.ui.theme.AppTheme
import uniffi.wpp_ffi.Night
import uniffi.wpp_ffi.SleepScore
import uniffi.wpp_ffi.SleepStage

@Composable
fun SleepScreen(
    night: Night?,
    window: LongRange,
    nowMs: Long,
    onShift: (Int) -> Unit,
    onBack: () -> Unit,
) {
    val asleep = night?.let { it.asleepFromMs to it.asleepToMs }
    DetailScaffold(
        title = "Sleep",
        subtitle = asleep?.let { (from, to) ->
            if (from == null || to == null) {
                "no sleep period detected"
            } else {
                "${dayName(to, nowMs)} · ${clock(from)} – ${clock(to)}"
            }
        } ?: "no night loaded",
        onBack = onBack,
        gap = AppTheme.space.blockLoose,
        trailing = { DayStepper(canGoForward = true, onStep = { onShift(-it) }) },
    ) {
        if (night == null || night.stages.isEmpty()) {
            EmptyNote(
                "The watch staged no sleep for this night. Stepping back finds " +
                    "the last night it did — nights it was off the wrist are " +
                    "skipped rather than shown as blanks."
            )
            return@DetailScaffold
        }

        val perStage = STAGE_ORDER.associateWith { stage ->
            night.stages.filter { it.stage == stage }.sumOf { it.toMs - it.fromMs }
        }
        val inBedMs = perStage.values.sum()
        val asleepMs = perStage.filterKeys { it != SleepStage.AWAKE }.values.sum()

        Column(
            Modifier.weight(1f).verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(AppTheme.space.blockLoose),
        ) {
            ScoreCard(night, asleepMs, inBedMs)

            Eyebrow("stages")
            Hypnogram(night.stages, window)

            Column(Modifier.fillMaxWidth()) {
                STAGE_ORDER.forEachIndexed { index, stage ->
                    if (index > 0) RowDivider(inset = 0.dp)
                    val total = perStage[stage] ?: 0L
                    Row(
                        Modifier.fillMaxWidth().padding(vertical = 7.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Box(
                            Modifier
                                .size(9.dp)
                                .clip(RoundedCornerShape(2.dp))
                                .background(stageColor(stage))
                        )
                        Text(
                            stageName(stage),
                            Modifier.weight(1f).padding(start = 9.dp),
                            style = AppTheme.type.rowTitle,
                        )
                        Text(
                            hoursMinutes(total),
                            style = AppTheme.type.rowMeta,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                        Text(
                            if (inBedMs > 0) "  ${100 * total / inBedMs}%" else "  —",
                            Modifier.width(48.dp),
                            style = AppTheme.type.rowMeta,
                            color = AppTheme.colors.onSurfaceTertiary,
                        )
                    }
                }
            }

            night.score?.let { QualityList(it) }
            Spacer(Modifier.height(8.dp))
        }
    }
}

@Composable
private fun ScoreCard(night: Night, asleepMs: Long, inBedMs: Long) {
    Column(
        Modifier.fillMaxWidth().padding(vertical = 6.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        val score = night.score
        Box(Modifier.size(SCORE_RING), contentAlignment = Alignment.Center) {
            ScoreRing(score?.total?.toInt() ?: 0)
            Text(
                score?.total?.toString() ?: "—",
                style = AppTheme.type.focalInverse,
                color = MaterialTheme.colorScheme.onSurface,
            )
        }
        Spacer(Modifier.height(10.dp))
        Text(
            buildString {
                append("${hoursMinutes(asleepMs)} asleep")
                val awakeMs = inBedMs - asleepMs
                val spells = night.stages.count { it.stage == SleepStage.AWAKE }
                if (awakeMs > WAKE_NOTABLE_MS || spells > WAKE_NOTABLE_SPELLS) {
                    append(
                        ". Woke up $spells ${if (spells == 1) "time" else "times"}, " +
                            "totaling ${compactDuration(awakeMs)}",
                    )
                } else if (awakeMs > 0) {
                    append(", ${compactDuration(awakeMs)} awake")
                }
                append(".")
            },
            style = AppTheme.type.rowTitle,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
        )
    }
}

private val SCORE_RING = 115.dp
private const val WAKE_NOTABLE_MS = 5 * 60_000L
private const val WAKE_NOTABLE_SPELLS = 5

@Composable
private fun ScoreRing(score: Int) {
    val track = AppTheme.colors.track
    val fill = MaterialTheme.colorScheme.primary
    Canvas(Modifier.size(SCORE_RING)) {
        val stroke = SCORE_RING.toPx() / 12
        val inset = stroke / 2
        val diameter = size.minDimension - stroke
        drawArc(
            track,
            startAngle = 0f,
            sweepAngle = 360f,
            useCenter = false,
            topLeft = Offset(inset, inset),
            size = Size(diameter, diameter),
            style = Stroke(width = stroke, cap = StrokeCap.Round),
        )
        drawArc(
            fill,
            startAngle = -90f,
            sweepAngle = 360f * (score.coerceIn(0, 100) / 100f),
            useCenter = false,
            topLeft = Offset(inset, inset),
            size = Size(diameter, diameter),
            style = Stroke(width = stroke, cap = StrokeCap.Round),
        )
    }
}

@Composable
private fun QualityList(score: SleepScore) {
    Tile(Modifier.fillMaxWidth()) {
        Text(
            "Score breakdown",
            Modifier.padding(bottom = 6.dp),
            style = AppTheme.type.sectionTitle,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        listOf(
            "Duration" to score.duration.toInt(),
            "Efficiency" to score.efficiency.toInt(),
            "Deep" to score.deep.toInt(),
            "REM" to score.rem.toInt(),
            "Continuity" to score.continuity.toInt(),
        ).forEach { (label, value) ->
            Row(
                Modifier.fillMaxWidth().padding(vertical = 6.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    label,
                    Modifier.width(70.dp),
                    style = AppTheme.type.rowMeta.copy(
                        fontFamily = androidx.compose.ui.text.font.FontFamily.Default,
                    ),
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                TrackBar(
                    value / 100f,
                    Modifier.weight(1f).padding(horizontal = 10.dp),
                    color = if (value < BELOW_PAR) {
                        AppTheme.colors.barBelow
                    } else {
                        MaterialTheme.colorScheme.primary
                    },
                )
                Text(
                    value.toString(),
                    Modifier.width(28.dp),
                    style = AppTheme.type.rowMeta,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

private const val BELOW_PAR = 50

fun Night.sleepWindow(): LongRange? {
    val from = asleepFromMs ?: return null
    val to = asleepToMs ?: return null
    return (from - SLEEP_MARGIN_MS)..(to + SLEEP_MARGIN_MS)
}

private const val SLEEP_MARGIN_MS = 10 * 60_000L
