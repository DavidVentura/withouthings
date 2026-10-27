package dev.davidv.withoutings.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.drawText
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.unit.dp
import dev.davidv.withoutings.ui.theme.AppTheme
import uniffi.wpp_ffi.EcgAnalysis
import uniffi.wpp_ffi.PulseAnalysis
import uniffi.wpp_ffi.RhythmAnalysis
import uniffi.wpp_ffi.RhythmPattern
import uniffi.wpp_ffi.WavePoint
import kotlin.math.abs

// Beat intervals the plots always span, so readings on one screen compare by
// eye: 300 ms is 200 bpm, 1500 ms is 40 bpm.
private const val AXIS_LOW_MS = 300f
private const val AXIS_HIGH_MS = 1500f
private val GUIDES_MS = listOf(500f, 1000f)
private const val RETURN_MAP_HALF_SPAN_MS = 400f

enum class Meaning(val label: String) {
    NORMAL("Normal"),
    EARLY_BEATS("Some early beats"),
    UNCLEAR("Unclear"),
    IRREGULAR("Irregular"),
    NOT_USABLE("Not usable"),
}

data class Verdict(val meaning: Meaning, val reason: String?)

private fun rhythmVerdict(rhythm: RhythmAnalysis, irregularReason: String): Verdict = when (rhythm.pattern) {
    RhythmPattern.BREATHING -> Verdict(Meaning.NORMAL, "varies with breathing")
    RhythmPattern.STEADY -> Verdict(Meaning.NORMAL, "steady")
    RhythmPattern.VARIABLE -> Verdict(Meaning.UNCLEAR, null)
    RhythmPattern.IRREGULAR -> Verdict(Meaning.IRREGULAR, irregularReason)
}

fun pulseVerdict(analysis: PulseAnalysis): Verdict = when (analysis) {
    is PulseAnalysis.Assessed -> rhythmVerdict(analysis.rhythm, "might be AFib, or the sensor moving")
    PulseAnalysis.Shifted -> Verdict(Meaning.NOT_USABLE, "heart rate changed mid-reading")
    PulseAnalysis.Disagrees -> Verdict(Meaning.NOT_USABLE, "the sensor miscounted beats")
    PulseAnalysis.TooFew -> Verdict(Meaning.NOT_USABLE, "too short")
    PulseAnalysis.NoWatchRate -> Verdict(Meaning.NOT_USABLE, "nothing to check it against")
}

fun ecgVerdict(analysis: EcgAnalysis): Verdict = when (analysis) {
    EcgAnalysis.TooFew -> Verdict(Meaning.NOT_USABLE, "too short")
    EcgAnalysis.Noisy -> Verdict(Meaning.NOT_USABLE, "too noisy")
    EcgAnalysis.Shifted -> Verdict(Meaning.NOT_USABLE, "heart rate changed mid-recording")
    is EcgAnalysis.Assessed -> when {
        // Detection misses some P waves on a clean sinus recording; none at all
        // is what fibrillating atria leave.
        analysis.pWaves == 0u -> Verdict(Meaning.IRREGULAR, "might be AFib")
        analysis.rhythm.pattern == RhythmPattern.IRREGULAR -> Verdict(Meaning.IRREGULAR, "might be AFib")
        analysis.earlyBeats > 0u -> Verdict(Meaning.EARLY_BEATS, "unlikely to be AFib")
        else -> rhythmVerdict(analysis.rhythm, "might be AFib")
    }
}

@Composable
fun VerdictText(verdict: Verdict) {
    Text(verdict.meaning.label, style = AppTheme.type.rowTitle)
    verdict.reason?.let {
        Text(it, style = AppTheme.type.body, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

// A breathing wave is shown by the sine it was tested with. Anything else is
// read from how each interval relates to the next.
@Composable
fun IntervalPlots(intervals: List<UInt>, rhythm: RhythmAnalysis?) {
    if (rhythm?.pattern == RhythmPattern.BREATHING) {
        IntervalPlot(intervals, rhythm.wave?.curve.orEmpty(), Modifier.fillMaxWidth().height(110.dp))
        return
    }
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        IntervalPlot(intervals, emptyList(), Modifier.weight(1f).height(110.dp))
        Spacer(Modifier.width(8.dp))
        ReturnMap(intervals, Modifier.size(110.dp))
    }
}

@Composable
private fun IntervalPlot(intervals: List<UInt>, wave: List<WavePoint>, modifier: Modifier) {
    val measurer = rememberTextMeasurer()
    val line = AppTheme.colors.dataStroke
    val waveColor = MaterialTheme.colorScheme.primary
    val grid = AppTheme.colors.chartGrid
    val labelStyle = AppTheme.type.axisSmall.copy(color = AppTheme.colors.onSurfaceDim)
    Canvas(modifier) {
        val y = { ms: Float ->
            size.height * (1f - (ms.coerceIn(AXIS_LOW_MS, AXIS_HIGH_MS) - AXIS_LOW_MS) / (AXIS_HIGH_MS - AXIS_LOW_MS))
        }
        GUIDES_MS.forEach { ms ->
            drawLine(grid, Offset(0f, y(ms)), Offset(size.width, y(ms)), strokeWidth = 1.dp.toPx())
            drawText(measurer, "${ms.toInt()} ms", Offset(0f, y(ms) - 14.dp.toPx()), labelStyle)
        }
        if (intervals.isEmpty()) return@Canvas
        val total = intervals.sumOf { it.toLong() }.toFloat()
        var at = 0f
        val points = intervals.map {
            at += it.toFloat()
            Offset(size.width * at / total, y(it.toFloat()))
        }
        val path = Path().apply {
            moveTo(points.first().x, points.first().y)
            points.drop(1).forEach { lineTo(it.x, it.y) }
        }
        drawPath(path, line.copy(alpha = 0.35f), style = Stroke(width = 1.dp.toPx()))
        if (wave.isNotEmpty()) {
            val curve = Path().apply {
                wave.forEachIndexed { index, point ->
                    val x = size.width * point.atMs.toFloat() / total
                    if (index == 0) moveTo(x, y(point.ms.toFloat())) else lineTo(x, y(point.ms.toFloat()))
                }
            }
            drawPath(curve, waveColor, style = Stroke(width = 1.5.dp.toPx()))
        }
        points.forEach { drawCircle(line, 2.5.dp.toPx(), it) }
    }
}

// Each interval against the next. A steady rhythm gathers on the diagonal,
// early beats sit off it in their own cluster, and an irregular rhythm
// scatters. Centred on the reading and at least a fixed span wide, so shapes
// still compare across readings while every interval stays inside.
@Composable
private fun ReturnMap(intervals: List<UInt>, modifier: Modifier) {
    val grid = AppTheme.colors.chartGrid
    val frame = MaterialTheme.colorScheme.surfaceContainerHigh
    val dot = AppTheme.colors.dataStroke
    val lengths = intervals.map { it.toFloat() }.sorted()
    val centre = if (lengths.isEmpty()) 0f else lengths[lengths.size / 2]
    val half = lengths.maxOfOrNull { abs(it - centre) }?.let { maxOf(it * 1.1f, RETURN_MAP_HALF_SPAN_MS) }
        ?: RETURN_MAP_HALF_SPAN_MS
    Canvas(modifier) {
        val at = { ms: Float -> (ms - centre) / (2 * half) + 0.5f }
        drawRect(frame, style = Stroke(width = 1.dp.toPx()))
        drawLine(grid, Offset(0f, size.height), Offset(size.width, 0f), strokeWidth = 1.dp.toPx())
        intervals.zipWithNext().forEach { (from, to) ->
            drawCircle(dot, 2.5.dp.toPx(), Offset(size.width * at(from.toFloat()), size.height * (1f - at(to.toFloat()))))
        }
    }
}
