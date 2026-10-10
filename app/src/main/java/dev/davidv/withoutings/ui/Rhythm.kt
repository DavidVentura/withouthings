package dev.davidv.withoutings.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Arrangement
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
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.drawText
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.unit.dp
import dev.davidv.withoutings.ui.theme.AppTheme
import uniffi.wpp_ffi.AfibReading
import uniffi.wpp_ffi.BeatInterval
import uniffi.wpp_ffi.BeatIntervalKind
import uniffi.wpp_ffi.PulseAnalysis
import uniffi.wpp_ffi.RhythmAnalysis
import uniffi.wpp_ffi.RhythmPattern
import uniffi.wpp_ffi.WavePoint
import kotlin.math.abs
import kotlin.math.roundToInt

// Beat intervals the plots always span, so readings on one screen compare by
// eye: 300 ms is 200 bpm, 1500 ms is 40 bpm.
private const val AXIS_LOW_MS = 300f
private const val AXIS_HIGH_MS = 1500f
private val GUIDES_MS = listOf(500f, 1000f)
private const val RETURN_MAP_HALF_SPAN_MS = 400f

fun patternLabel(pattern: RhythmPattern): String = when (pattern) {
    RhythmPattern.STEADY -> "Steady"
    RhythmPattern.BREATHING -> "Breathing rhythm"
    RhythmPattern.VARIABLE -> "Variable"
    RhythmPattern.IRREGULAR -> "Irregular"
}

fun rhythmLabel(rhythm: RhythmAnalysis): String = listOfNotNull(
    patternLabel(rhythm.pattern),
    earlyBeats(rhythm.earlyBeats.toInt()),
).joinToString(" · ")

fun pulseLabel(analysis: PulseAnalysis): String = when (analysis) {
    is PulseAnalysis.Assessed -> rhythmLabel(analysis.rhythm)
    is PulseAnalysis.Disagrees -> "Unreadable"
    is PulseAnalysis.Shifted -> "Rate changed"
    PulseAnalysis.TooFew -> "Too short"
    PulseAnalysis.NoWatchRate -> "Not checked"
}

private fun earlyBeats(count: Int): String? = when (count) {
    0 -> null
    1 -> "1 early beat"
    else -> "$count early beats"
}

fun rhythmLines(rhythm: RhythmAnalysis, source: RhythmSource): List<String> {
    val wave = rhythm.wave
    val pattern = when (rhythm.pattern) {
        RhythmPattern.STEADY -> "The beats keep close to an even pace."
        RhythmPattern.BREATHING -> "Speeds up and slows down with each breath, the way the heart does at rest."
        RhythmPattern.VARIABLE ->
            "Varies more than a steady rhythm, and no breathing wave accounts for most of it."
        RhythmPattern.IRREGULAR -> when (source) {
            RhythmSource.Pulse ->
                "The intervals jump with no pattern. AFib looks like this on a pulse sensor, " +
                    "and so does a sensor that moved on the wrist."
            RhythmSource.Ecg -> "The intervals jump with no pattern, the way they do in AFib."
        }
    }
    if (wave == null || rhythm.pattern != RhythmPattern.BREATHING) return listOf(pattern)
    val shuffled = (wave.chance * 100).roundToInt()
    return listOf(
        pattern,
        "A wave of about ${wave.perMinute.roundToInt()} breaths a minute accounts for " +
            "${(wave.share * 100).roundToInt()}% of how the beats vary.",
        if (shuffled < 1) {
            "Fewer than 1 in 100 shuffled orders of the same beats fit a wave as well."
        } else {
            "$shuffled in 100 shuffled orders of the same beats fit a wave as well."
        },
    )
}

enum class RhythmSource { Pulse, Ecg }

fun shiftLine(from: UInt, to: UInt): String =
    "The rate moved from $from to $to bpm partway through, so its pattern is not judged."

fun pulseLines(reading: AfibReading): List<String> = when (val analysis = reading.analysis) {
    is PulseAnalysis.Assessed -> rhythmLines(analysis.rhythm, RhythmSource.Pulse) +
        listOfNotNull(
            reading.watchBreathsPerMinute
                ?.takeIf { analysis.rhythm.pattern == RhythmPattern.BREATHING }
                ?.let { "The watch logged $it breaths a minute around then." }
        )
    is PulseAnalysis.Disagrees -> listOf(
        "It counts ${analysis.counted} bpm where the watch measured ${analysis.watch} bpm " +
            "for the same minute, so its beats cannot be trusted."
    )
    is PulseAnalysis.Shifted -> listOf(shiftLine(analysis.from, analysis.to))
    PulseAnalysis.TooFew -> listOf("Too few beats to tell a pattern from chance.")
    PulseAnalysis.NoWatchRate -> listOf("The watch logged no heart rate to check its beats against.")
}

@Composable
fun SpeculativeEyebrow() {
    Text(
        "Our reading · speculative",
        style = AppTheme.type.rowMeta,
        color = AppTheme.colors.onSurfaceTertiary,
    )
}

// A breathing wave is shown by the sine it was tested with. Anything else is
// read from how each interval relates to the next.
@Composable
fun IntervalPlots(intervals: List<BeatInterval>, rhythm: RhythmAnalysis?) {
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
private fun kindColor(kind: BeatIntervalKind): Color = when (kind) {
    BeatIntervalKind.BEAT -> AppTheme.colors.dataStroke
    BeatIntervalKind.MERGED -> AppTheme.colors.onSurfaceDim
    BeatIntervalKind.EARLY, BeatIntervalKind.PAUSE -> MaterialTheme.colorScheme.primary
}

@Composable
private fun IntervalPlot(intervals: List<BeatInterval>, wave: List<WavePoint>, modifier: Modifier) {
    val measurer = rememberTextMeasurer()
    val line = AppTheme.colors.dataStroke
    val waveColor = MaterialTheme.colorScheme.primary
    val grid = AppTheme.colors.chartGrid
    val labelStyle = AppTheme.type.axisSmall.copy(color = AppTheme.colors.onSurfaceDim)
    val colors = intervals.map { kindColor(it.kind) }
    Canvas(modifier) {
        val y = { ms: Float ->
            size.height * (1f - (ms.coerceIn(AXIS_LOW_MS, AXIS_HIGH_MS) - AXIS_LOW_MS) / (AXIS_HIGH_MS - AXIS_LOW_MS))
        }
        GUIDES_MS.forEach { ms ->
            drawLine(grid, Offset(0f, y(ms)), Offset(size.width, y(ms)), strokeWidth = 1.dp.toPx())
            drawText(measurer, "${ms.toInt()} ms", Offset(0f, y(ms) - 14.dp.toPx()), labelStyle)
        }
        if (intervals.isEmpty()) return@Canvas
        val total = intervals.sumOf { it.ms.toLong() }.toFloat()
        var at = 0f
        val points = intervals.map {
            at += it.ms.toFloat()
            Offset(size.width * at / total, y(it.ms.toFloat()))
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
        points.zip(intervals).zip(colors).forEach { (pair, color) ->
            val (point, interval) = pair
            if (interval.kind == BeatIntervalKind.MERGED) {
                drawCircle(color, 3.dp.toPx(), point, style = Stroke(width = 1.2.dp.toPx()))
            } else {
                drawCircle(color, 2.5.dp.toPx(), point)
            }
        }
    }
}

// Each interval against the next. A steady rhythm gathers on the diagonal,
// early beats sit off it in their own cluster, and an irregular rhythm
// scatters. Centred on the reading and at least a fixed span wide, so shapes
// still compare across readings while every interval stays inside.
@Composable
private fun ReturnMap(intervals: List<BeatInterval>, modifier: Modifier) {
    val grid = AppTheme.colors.chartGrid
    val frame = MaterialTheme.colorScheme.surfaceContainerHigh
    val colors = intervals.map { kindColor(it.kind) }
    val lengths = intervals.map { it.ms.toFloat() }.sorted()
    val centre = if (lengths.isEmpty()) 0f else lengths[lengths.size / 2]
    val half = lengths.maxOfOrNull { abs(it - centre) }?.let { maxOf(it * 1.1f, RETURN_MAP_HALF_SPAN_MS) }
        ?: RETURN_MAP_HALF_SPAN_MS
    Canvas(modifier) {
        val at = { ms: Float -> (ms - centre) / (2 * half) + 0.5f }
        drawRect(frame, style = Stroke(width = 1.dp.toPx()))
        drawLine(grid, Offset(0f, size.height), Offset(size.width, 0f), strokeWidth = 1.dp.toPx())
        intervals.zipWithNext().forEachIndexed { index, (from, to) ->
            val early = colors[index + 1].takeIf { to.kind != BeatIntervalKind.BEAT && to.kind != BeatIntervalKind.MERGED }
            drawCircle(
                early ?: colors[index],
                2.5.dp.toPx(),
                Offset(size.width * at(from.ms.toFloat()), size.height * (1f - at(to.ms.toFloat()))),
            )
        }
    }
}

@Composable
fun IntervalLegend(intervals: List<BeatInterval>) {
    val merged = intervals.count { it.kind == BeatIntervalKind.MERGED }
    val early = intervals.any { it.kind == BeatIntervalKind.EARLY }
    if (merged == 0 && !early) return
    Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        if (early) LegendSwatch(MaterialTheme.colorScheme.primary, "early beat and its pause")
        if (merged > 0) {
            LegendSwatch(
                AppTheme.colors.onSurfaceDim,
                if (merged == 1) "1 pulse counted twice, merged" else "$merged pulses counted twice, merged",
            )
        }
    }
}
