package dev.davidv.withoutings.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
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
import dev.davidv.withoutings.ui.theme.AppTheme
import uniffi.wpp_ffi.SleepBand
import uniffi.wpp_ffi.SleepStage

private val lanes = listOf(SleepStage.AWAKE, SleepStage.REM, SleepStage.LIGHT, SleepStage.DEEP)

val SLEEP_GUTTER = 42.dp

fun stageName(stage: SleepStage): String = when (stage) {
    SleepStage.AWAKE -> "Awake"
    SleepStage.REM -> "REM"
    SleepStage.LIGHT -> "Light"
    SleepStage.DEEP -> "Deep"
}

val STAGE_ORDER = listOf(SleepStage.DEEP, SleepStage.REM, SleepStage.LIGHT, SleepStage.AWAKE)

@Composable
fun stageColor(stage: SleepStage): Color {
    val colors = AppTheme.colors
    return when (stage) {
        SleepStage.DEEP -> colors.sleepDeep
        SleepStage.REM -> colors.sleepRem
        SleepStage.LIGHT -> colors.sleepLight
        SleepStage.AWAKE -> colors.sleepAwake
    }
}

@Composable
fun Hypnogram(
    stages: List<SleepBand>,
    window: LongRange,
    modifier: Modifier = Modifier,
    height: Dp = 138.dp,
    gutterLeftDp: Dp = SLEEP_GUTTER,
) {
    val measurer = rememberTextMeasurer()
    val colors = lanes.associateWith { stageColor(it) }
    val labelStyle = AppTheme.type.axisSmall.copy(color = AppTheme.colors.onSurfaceDim)
    val gridColor = AppTheme.colors.chartGrid

    Box(modifier.fillMaxWidth().height(height)) {
        Canvas(Modifier.fillMaxWidth().height(height)) {
            val gutterLeft = gutterLeftDp.toPx()
            val plotWidth = size.width - gutterLeft
            val spanMs = (window.last - window.first).coerceAtLeast(1L).toFloat()
            val axisHeight = 16.dp.toPx()
            val plotHeight = size.height - axisHeight
            val laneHeight = plotHeight / lanes.size
            val barHeight = laneHeight * 0.62f

            fun x(at: Long) = gutterLeft + ((at - window.first).toFloat() / spanMs) * plotWidth

            for (tick in timeTicks(window)) {
                val at = x(tick)
                drawLine(gridColor, Offset(at, 0f), Offset(at, plotHeight), 1f)
                val label = measurer.measure(timeTickLabel(tick, window), labelStyle)
                val left = (at - label.size.width / 2)
                    .coerceIn(gutterLeft, (size.width - label.size.width).coerceAtLeast(gutterLeft))
                drawText(label, topLeft = Offset(left, plotHeight + 3f))
            }

            lanes.forEachIndexed { row, stage ->
                val mid = laneHeight * row + laneHeight / 2
                drawLine(gridColor, Offset(gutterLeft, mid), Offset(size.width, mid), 1f)
                val label = measurer.measure(stageName(stage), labelStyle)
                drawText(
                    label,
                    topLeft = Offset(
                        gutterLeft - label.size.width - 6f,
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
                    size = Size((right - left).coerceAtLeast(2f), barHeight),
                    cornerRadius = CornerRadius(2f, 2f),
                )
            }
        }
    }
}
