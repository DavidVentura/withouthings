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
import androidx.compose.ui.unit.dp
import dev.davidv.withoutings.ui.theme.AppTheme
import uniffi.wpp_ffi.Night
import uniffi.wpp_ffi.SleepScore
import uniffi.wpp_ffi.SleepStage

/**
 * One night, read.
 *
 * Everything on the screen comes from the watch's own staging. A window it did
 * not stage produces no band rather than a guess, which is why the empty state
 * says so instead of drawing a flat line.
 */
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

/**
 * The score, and what it is made of.
 *
 * The sentence beside the ring is assembled from the components rather than
 * written: a total can then be argued with, which is the point of showing the
 * parts at all.
 */
@Composable
private fun ScoreCard(night: Night, asleepMs: Long, inBedMs: Long) {
    AccentCard(Modifier.fillMaxWidth(), shape = RoundedCornerShape(AppTheme.radius.hero)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            val score = night.score
            Box(Modifier.size(96.dp), contentAlignment = Alignment.Center) {
                ScoreRing(score?.total?.toInt() ?: 0)
                Text(
                    score?.total?.toString() ?: "—",
                    style = AppTheme.type.summaryValue,
                    color = MaterialTheme.colorScheme.onPrimaryContainer,
                )
            }
            Column(Modifier.weight(1f).padding(start = 18.dp)) {
                Text(
                    scoreHeadline(night.score),
                    style = AppTheme.type.cardTitle,
                    color = MaterialTheme.colorScheme.onPrimaryContainer,
                )
                Spacer(Modifier.height(3.dp))
                Text(
                    buildString {
                        append("${hoursMinutes(asleepMs)} asleep")
                        if (inBedMs > asleepMs) {
                            append(", ${compactDuration(inBedMs - asleepMs)} awake")
                        }
                        append(" across ${night.stages.size} stretches.")
                    },
                    style = AppTheme.type.bodyLarge,
                    color = AppTheme.colors.onAccentSecondary,
                )
            }
        }
    }
}

/**
 * The night named by its own components.
 *
 * Nothing here interprets: it reports which part of the score came out
 * strongest and which weakest, which is a fact about the number beside it.
 */
private fun scoreHeadline(score: SleepScore?): String {
    if (score == null) return "No score for this night"
    val parts = listOf(
        "duration" to score.duration.toInt(),
        "efficiency" to score.efficiency.toInt(),
        "deep" to score.deep.toInt(),
        "REM" to score.rem.toInt(),
        "continuity" to score.continuity.toInt(),
    )
    val best = parts.maxBy { it.second }
    val worst = parts.minBy { it.second }
    if (best.second - worst.second < EVEN_NIGHT_SPREAD) return "An even night"
    return "${best.first.replaceFirstChar { it.uppercase() }} led, ${worst.first} lagged"
}

/// Under this, no component stands out enough to be worth naming.
private const val EVEN_NIGHT_SPREAD = 15

@Composable
private fun ScoreRing(score: Int) {
    val track = AppTheme.colors.ringTrack
    val fill = MaterialTheme.colorScheme.primary
    Canvas(Modifier.size(96.dp)) {
        val stroke = 8.dp.toPx()
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

/**
 * What the score is made of.
 *
 * A component that came out below par gets a neutral bar, never a warm one:
 * this app does not tell anyone their night was a problem.
 */
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

/// Where a component stops being drawn in the primary colour. Neutral below,
/// never warm.
private const val BELOW_PAR = 50

/**
 * The window the sleep charts open on.
 *
 * The night is fetched far wider than this — evening through late morning,
 * because the heart-rate detection takes its levels from the waking hours —
 * but those hours are not what the screen is for.
 */
fun Night.sleepWindow(): LongRange? {
    val from = asleepFromMs ?: return null
    val to = asleepToMs ?: return null
    return (from - SLEEP_MARGIN_MS)..(to + SLEEP_MARGIN_MS)
}

/// Enough either side to see the night end, not enough to lose it in the day.
private const val SLEEP_MARGIN_MS = 10 * 60_000L
