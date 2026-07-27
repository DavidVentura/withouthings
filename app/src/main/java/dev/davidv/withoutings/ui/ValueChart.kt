package dev.davidv.withoutings.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.gestures.calculateCentroid
import androidx.compose.foundation.gestures.calculatePan
import androidx.compose.foundation.gestures.calculateZoom
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.runtime.Composable
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.Fill
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.clipRect
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.text.TextMeasurer
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.drawText
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.unit.Dp
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
    /// Fixed vertical range for the series, in its own units. A scale that
    /// refits itself to whatever is on screen makes the same reading look
    /// different depending on where you scrolled from.
    axis: ClosedFloatingPointRange<Double>,
    decimals: Int,
    /// How far the view may be panned. A finished workout is bounded at both
    /// ends by its own extent; an open-ended series only by the present.
    limit: LongRange? = null,
    modifier: Modifier = Modifier,
    height: Dp = 260.dp,
    lineColor: Color = Color(0xFF4C7EF3),
    gridColor: Color = Color(0x22000000),
    axisColor: Color = Color(0x66000000),
    labelColor: Color = Color(0x99000000),
    setColor: Color = Color(0x22448AFF),
) {
    val measurer = rememberTextMeasurer()
    // Read inside the gesture without keying the handler on it: keying on the
    // window restarts pointerInput on the first emission, which cancels the
    // pinch in progress and limits a gesture to a single step.
    val latest = rememberUpdatedState(window)
    val bounds = rememberUpdatedState(limit)
    val gutterLeftDp = 34.dp

    Box(
        modifier
            .fillMaxWidth()
            .height(height)
            .pointerInput(Unit) {
                val gutter = gutterLeftDp.toPx()
                awaitEachGesture {
                    awaitFirstDown(requireUnconsumed = false)
                    // The window a gesture works from is its own, so a slow
                    // recomposition cannot feed a stale value back in.
                    var live = latest.value
                    do {
                        val event = awaitPointerEvent()
                        val zoom = event.calculateZoom()
                        val pan = event.calculatePan()
                        if (zoom == 1f && pan.x == 0f) continue

                        val plotWidth = (size.width - gutter).coerceAtLeast(1f)
                        val span = (live.last - live.first).coerceAtLeast(1_000L)
                        val bound = bounds.value
                        val newest = bound?.last ?: System.currentTimeMillis()
                        val oldest = bound?.first
                        // Ten seconds to a year: clamping to a day snapped every
                        // longer window back on the first touch.
                        var scaled = (span / zoom).toLong()
                            .coerceIn(10_000L, 365L * 24 * 3600 * 1000)
                        // Zooming out past the whole of a bounded series only
                        // adds empty margin either side of it.
                        if (oldest != null) scaled = scaled.coerceAtMost(newest - oldest)

                        // Anchor on the pinch centroid so the instant under the
                        // fingers stays under them, rather than scaling about
                        // the middle of a window nobody is touching.
                        val anchor = ((event.calculateCentroid(useCurrent = true).x - gutter)
                            / plotWidth).coerceIn(0f, 1f)
                        val at = live.first + (span * anchor).toLong()
                        val shift = -(pan.x / plotWidth * scaled).toLong()
                        var first = at - (scaled * anchor).toLong() + shift

                        // Panning past either edge only scrolls into empty
                        // space you then have to find your way back from.
                        if (first + scaled > newest) first = newest - scaled
                        if (oldest != null && first < oldest) first = oldest

                        live = first..(first + scaled)
                        onWindowChange(live)
                        event.changes.forEach { it.consume() }
                    } while (event.changes.any { it.pressed })
                }
            }
    ) {
        Canvas(Modifier.fillMaxWidth().height(height)) {
            val gutterLeft = gutterLeftDp.toPx()
            val gutterBottom = 18.dp.toPx()
            // Widths are dp, not raw pixels: at 452dpi a 1f line is a third of
            // a dp, which is the hairline that made every chart look unstyled.
            val hairline = 1.dp.toPx() / 2
            val axisWidth = 1.dp.toPx()
            val lineWidth = 2.dp.toPx()
            val plotWidth = size.width - gutterLeft
            val plotHeight = size.height - gutterBottom
            if (plotWidth <= 0 || plotHeight <= 0) return@Canvas

            val (lo, hi) = bounds(points.filter { it.atMs in window }, axis)
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
                drawLine(gridColor, Offset(gutterLeft, at), Offset(size.width, at), hairline)
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
                drawLine(gridColor, Offset(at, 0f), Offset(at, plotHeight), hairline)
                val label = measurer.measure(
                    format.format(Date(tick)),
                    TextStyle(fontSize = 9.sp, color = labelColor),
                )
                val left = (at - label.size.width / 2)
                    .coerceIn(gutterLeft, size.width - label.size.width)
                drawText(label, topLeft = Offset(left, plotHeight + 3f))
                tick += tickMs
            }

            drawLine(axisColor, Offset(gutterLeft, 0f), Offset(gutterLeft, plotHeight), axisWidth)
            drawLine(
                axisColor,
                Offset(gutterLeft, plotHeight),
                Offset(size.width, plotHeight),
                axisWidth,
            )

            if (points.isEmpty()) return@Canvas
            val ordered = points.sortedBy { it.atMs }
            val path = Path()
            ordered.forEachIndexed { index, point ->
                val px = x(point.atMs)
                val py = y(point.value)
                if (index == 0) path.moveTo(px, py) else path.lineTo(px, py)
            }

            // The series deliberately reaches one point beyond each edge so the
            // line arrives from off-screen instead of stopping at the plot; the
            // clip is what keeps that off the axis and the labels.
            clipRect(left = gutterLeft, top = 0f, right = size.width, bottom = plotHeight) {
                // Closing the trace back along the baseline gives the line some
                // visual weight without competing with it.
                val fill = Path().apply {
                    addPath(path)
                    lineTo(x(ordered.last().atMs), plotHeight)
                    lineTo(x(ordered.first().atMs), plotHeight)
                    close()
                }
                drawPath(fill, lineColor.copy(alpha = 0.12f), style = Fill)
                drawPath(
                    path,
                    lineColor,
                    style = Stroke(
                        width = lineWidth,
                        cap = StrokeCap.Round,
                        join = StrokeJoin.Round,
                    ),
                )

                // A daily total gives one point per day; a line between two of
                // them is invisible and a single one draws nothing at all.
                if (ordered.size <= 60) {
                    ordered.forEach {
                        drawCircle(
                            lineColor,
                            radius = lineWidth,
                            center = Offset(x(it.atMs), y(it.value)),
                        )
                    }
                }
            }
        }
    }
}

/**
 * The series' fixed range, widened only if the data leaves it.
 *
 * Fitting the axis to whatever is on screen means a reading of 70 sits at a
 * different height depending on what else the window happens to contain, and
 * the whole plot jumps as you scroll. The range is fixed instead; growing it
 * for an out-of-range value is what stops a fixed axis from hiding data.
 */
private fun bounds(
    points: List<ChartPoint>,
    axis: ClosedFloatingPointRange<Double>,
): Pair<Double, Double> {
    var lo = axis.start
    var hi = axis.endInclusive
    if (points.isNotEmpty()) {
        lo = minOf(lo, points.minOf { it.value })
        hi = maxOf(hi, points.maxOf { it.value })
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
