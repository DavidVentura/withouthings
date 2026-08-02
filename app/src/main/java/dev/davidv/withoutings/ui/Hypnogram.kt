package dev.davidv.withoutings.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.drawText
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import uniffi.wpp_ffi.SleepBand
import uniffi.wpp_ffi.SleepStage

/**
 * The stages in the order a hypnogram draws them, deepest at the bottom.
 * Not the wire order, which is awake, light, deep, REM.
 */
private val lanes = listOf(SleepStage.AWAKE, SleepStage.REM, SleepStage.LIGHT, SleepStage.DEEP)

/**
 * Wide enough for "Awake" beside the lanes, and used by every chart on the
 * sleep screen so they share one time axis.
 */
val SLEEP_GUTTER = 46.dp

fun stageName(stage: SleepStage): String = when (stage) {
    SleepStage.AWAKE -> "Awake"
    SleepStage.REM -> "REM"
    SleepStage.LIGHT -> "Light"
    SleepStage.DEEP -> "Deep"
}

@Composable
fun stageColor(stage: SleepStage): Color {
    val scheme = MaterialTheme.colorScheme
    return when (stage) {
        SleepStage.AWAKE -> scheme.outline
        SleepStage.REM -> scheme.tertiary
        SleepStage.LIGHT -> scheme.primary.copy(alpha = 0.45f)
        SleepStage.DEEP -> scheme.primary
    }
}

/**
 * The watch's own staging over the same window the other charts draw, so the
 * three line up when one of them is panned.
 *
 * Deliberately not interactive: the gestures live on the value charts, and this
 * follows the window they publish.
 */
@Composable
fun Hypnogram(
    stages: List<SleepBand>,
    window: LongRange,
    modifier: Modifier = Modifier,
    height: Dp = 110.dp,
    /// Must match the value charts above and below, or the same instant sits at
    /// a different x in each.
    gutterLeftDp: Dp = SLEEP_GUTTER,
) {
    val measurer = rememberTextMeasurer()
    val colors = lanes.associateWith { stageColor(it) }
    val labelColor = MaterialTheme.colorScheme.onSurfaceVariant
    val gridColor = MaterialTheme.colorScheme.outlineVariant

    Box(modifier.fillMaxWidth().height(height)) {
        Canvas(Modifier.fillMaxWidth().height(height)) {
            val gutterLeft = gutterLeftDp.toPx()
            val plotWidth = size.width - gutterLeft
            val spanMs = (window.last - window.first).coerceAtLeast(1L).toFloat()
            val laneHeight = size.height / lanes.size
            val barHeight = laneHeight * 0.62f

            fun x(at: Long) = gutterLeft + ((at - window.first).toFloat() / spanMs) * plotWidth

            lanes.forEachIndexed { row, stage ->
                val mid = laneHeight * row + laneHeight / 2
                drawLine(gridColor, Offset(gutterLeft, mid), Offset(size.width, mid), 1f)
                val label = measurer.measure(
                    stageName(stage),
                    TextStyle(color = labelColor, fontSize = 10.sp),
                )
                drawText(
                    label,
                    topLeft = Offset(
                        gutterLeft - label.size.width - 4f,
                        mid - label.size.height / 2,
                    ),
                )
            }

            for (band in stages) {
                if (band.toMs < window.first || band.fromMs > window.last) continue
                val row = lanes.indexOf(band.stage)
                if (row < 0) continue
                val left = x(band.fromMs).coerceAtLeast(gutterLeft)
                val right = x(band.toMs).coerceAtMost(size.width)
                val mid = laneHeight * row + laneHeight / 2
                drawRoundRect(
                    color = colors.getValue(band.stage),
                    topLeft = Offset(left, mid - barHeight / 2),
                    // A one-minute window at a night's zoom is under a pixel
                    // wide, and a stage that is invisible reads as absent.
                    size = Size((right - left).coerceAtLeast(2f), barHeight),
                    cornerRadius = CornerRadius(2f, 2f),
                )
            }
        }
    }
}
