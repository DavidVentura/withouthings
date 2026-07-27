package dev.davidv.withoutings.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.gestures.detectTransformGestures
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.text.TextMeasurer
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.drawText
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.math.ceil
import kotlin.math.floor
import uniffi.wpp_ffi.Marker
import uniffi.wpp_ffi.SetEdge

/** A value at a time, whatever the series. */
data class ChartPoint(val atMs: Long, val value: Double)

private val TICK_SECONDS = listOf(
    10, 30, 60, 120, 300, 600, 1800, 3600, 7200, 21600, 43200,
    86400, 2 * 86400, 7 * 86400, 30 * 86400,
)

/** Axis steps that read cleanly, scaled by powers of ten to fit any range. */
private val NICE_STEPS = listOf(1.0, 2.0, 5.0)

private val hms = SimpleDateFormat("HH:mm:ss", Locale.getDefault())
private val hm = SimpleDateFormat("HH:mm", Locale.getDefault())
private val dm = SimpleDateFormat("d MMM", Locale.getDefault())

/**
 * Heart rate over a window, with set intervals shaded behind it.
 *
 * Pinch to zoom, drag to pan; both move the window handed to Rust rather than
 * scaling a bitmap, so zooming in fetches finer data instead of magnifying what
 * was already reduced.
 */
@Composable
fun ValueChart(
    points: List<ChartPoint>,
    markers: List<Marker>,
    window: LongRange,
    onWindowChange: (LongRange) -> Unit,
    /// Smallest vertical range to show, so steady data does not get magnified
    /// into noise. In the series' own units.
    minSpan: Double,
    decimals: Int,
    modifier: Modifier = Modifier,
    lineColor: Color = Color(0xFF4C7EF3),
    gridColor: Color = Color(0x22000000),
    axisColor: Color = Color(0x66000000),
    labelColor: Color = Color(0x99000000),
    setColor: Color = Color(0x22448AFF),
) {
    val measurer = rememberTextMeasurer()
    Box(
        modifier
            .fillMaxWidth()
            .height(260.dp)
            .pointerInput(window) {
                detectTransformGestures { _, pan, zoom, _ ->
                    val span = (window.last - window.first).coerceAtLeast(1_000L)
                    // Ten seconds to a year: clamping to a day snapped every
                    // longer window back on the first touch.
                    val scaled = (span / zoom).toLong()
                        .coerceIn(10_000L, 365L * 24 * 3600 * 1000)
                    val shift = -(pan.x / size.width * scaled).toLong()
                    val centre = (window.first + window.last) / 2 + shift
                    onWindowChange((centre - scaled / 2)..(centre + scaled / 2))
                }
            }
    ) {
        Canvas(Modifier.fillMaxWidth().height(260.dp)) {
            val gutterLeft = 34.dp.toPx()
            val gutterBottom = 18.dp.toPx()
            val plotWidth = size.width - gutterLeft
            val plotHeight = size.height - gutterBottom
            if (plotWidth <= 0 || plotHeight <= 0) return@Canvas

            val (lo, hi) = bounds(points, minSpan)
            val spanMs = (window.last - window.first).coerceAtLeast(1L)

            fun x(at: Long) = gutterLeft + ((at - window.first).toFloat() / spanMs) * plotWidth
            fun y(value: Double) =
                plotHeight - ((value - lo) / (hi - lo)).toFloat() * plotHeight

            var open: Long? = null
            for (marker in markers.sortedBy { it.atMs }) {
                when (marker.edge) {
                    SetEdge.START -> open = marker.atMs
                    SetEdge.END -> {
                        val from = open ?: continue
                        drawRect(
                            setColor,
                            Offset(x(from), 0f),
                            Size((x(marker.atMs) - x(from)).coerceAtLeast(1f), plotHeight),
                        )
                        open = null
                    }
                }
            }
            open?.let {
                drawRect(
                    setColor,
                    Offset(x(it), 0f),
                    Size((size.width - x(it)).coerceAtLeast(1f), plotHeight),
                )
            }

            val step = niceStep(hi - lo)
            var value = ceil(lo / step) * step
            while (value <= hi + step / 1000) {
                val at = y(value)
                drawLine(gridColor, Offset(gutterLeft, at), Offset(size.width, at), 1f)
                val label = measurer.measure(
                    formatValue(value, decimals),
                    TextStyle(fontSize = 9.sp, color = labelColor),
                )
                drawText(
                    label,
                    topLeft = Offset(gutterLeft - label.size.width - 4f, at - label.size.height / 2),
                )
                value += step
            }

            // Time ticks land on round wall-clock instants so the labels stay
            // put while panning instead of sliding with the data.
            // Falling back to an hour put 168 gridlines on a week; fall back to
            // the coarsest step instead.
            val tickSeconds =
                TICK_SECONDS.firstOrNull { spanMs / 1000 / it <= 6 } ?: TICK_SECONDS.last()
            val tickMs = tickSeconds * 1000L
            val format = when {
                spanMs > 36 * 3600_000L -> dm
                spanMs > 30 * 60_000L -> hm
                else -> hms
            }
            var tick = (window.first / tickMs) * tickMs
            if (tick < window.first) tick += tickMs
            while (tick <= window.last) {
                val at = x(tick)
                drawLine(gridColor, Offset(at, 0f), Offset(at, plotHeight), 1f)
                val label = measurer.measure(
                    format.format(Date(tick)),
                    TextStyle(fontSize = 9.sp, color = labelColor),
                )
                val left = (at - label.size.width / 2)
                    .coerceIn(gutterLeft, size.width - label.size.width)
                drawText(label, topLeft = Offset(left, plotHeight + 3f))
                tick += tickMs
            }

            drawLine(axisColor, Offset(gutterLeft, 0f), Offset(gutterLeft, plotHeight), 1.5f)
            drawLine(
                axisColor,
                Offset(gutterLeft, plotHeight),
                Offset(size.width, plotHeight),
                1.5f,
            )

            if (points.isEmpty()) return@Canvas
            val path = Path()
            points.sortedBy { it.atMs }.forEachIndexed { index, point ->
                val px = x(point.atMs)
                val py = y(point.value)
                if (index == 0) path.moveTo(px, py) else path.lineTo(px, py)
            }
            drawPath(path, lineColor, style = Stroke(width = 2.5f))

            // A daily total gives one point per day; a line between two of them
            // is invisible and a single one draws nothing at all.
            if (points.size <= 60) {
                points.forEach {
                    drawCircle(lineColor, radius = 3.5f, center = Offset(x(it.atMs), y(it.value)))
                }
            }
        }
    }
}

/**
 * Padded bounds with a floor on the span.
 *
 * Fitting the axis tightly to the data turns a little jitter into a
 * dramatic-looking swing, which is the opposite of useful.
 */
private fun bounds(points: List<ChartPoint>, minSpan: Double): Pair<Double, Double> {
    if (points.isEmpty()) return 0.0 to minSpan
    val min = points.minOf { it.value }
    val max = points.maxOf { it.value }
    val padding = ((max - min) * 0.1).coerceAtLeast(minSpan * 0.1)
    var lo = min - padding
    var hi = max + padding
    val short = minSpan - (hi - lo)
    if (short > 0) {
        lo -= short / 2
        hi += short / 2
    }
    val step = niceStep(hi - lo)
    return floor(lo / step) * step to ceil(hi / step) * step
}

/** A step that divides the range into at most six readable intervals. */
private fun niceStep(span: Double): Double {
    if (span <= 0) return 1.0
    val magnitude = Math.pow(10.0, floor(kotlin.math.log10(span / 6)))
    return NICE_STEPS.map { it * magnitude }.firstOrNull { span / it <= 6 }
        ?: (10 * magnitude)
}

private fun formatValue(value: Double, decimals: Int): String =
    if (decimals == 0) value.toInt().toString()
    else String.format(Locale.US, "%.${decimals}f", value)
