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

/** A stretch of time shaded behind the trace. A null end runs to the edge. */
data class Band(val fromMs: Long, val toMs: Long?, val color: Color)

/** Start/end markers as bands. An unclosed start runs to the edge of the view. */
fun List<Marker>.bands(color: Color): List<Band> {
    val out = mutableListOf<Band>()
    var open: Long? = null
    for (marker in sortedBy { it.atMs }) {
        when (marker.edge) {
            SetEdge.START -> open = marker.atMs
            SetEdge.END -> open?.let {
                out.add(Band(it, marker.atMs, color))
                open = null
            }
        }
    }
    open?.let { out.add(Band(it, null, color)) }
    return out
}

/**
 * How the background is ruled.
 *
 * A trend line wants readable round numbers wherever the axis happens to land.
 * An ECG wants the paper it has always been printed on — 25 mm/s and 10 mm/mV,
 * so a large square is 0.2 s by 0.5 mV — because that is what makes an interval
 * measurable by eye.
 */
enum class GridStyle { Time, EcgPaper }

private val TICK_SECONDS = listOf(
    10, 30, 60, 120, 300, 600, 1800, 3600, 7200, 21600, 43200,
    86400, 2 * 86400, 7 * 86400, 30 * 86400,
)

/// ECG paper at 25 mm/s and 10 mm/mV: a large square is 5 mm each way.
private const val ECG_MS_PER_LARGE_SQUARE = 200L
private const val ECG_MS_PER_SMALL_SQUARE = 40L
private const val ECG_MV_PER_LARGE_SQUARE = 0.5
private const val ECG_MV_PER_SMALL_SQUARE = 0.1

/** Axis steps that read cleanly, scaled by powers of ten to fit any range. */
private val NICE_STEPS = listOf(1.0, 2.0, 5.0)

private val hms = SimpleDateFormat("HH:mm:ss", Locale.getDefault())
private val hm = SimpleDateFormat("HH:mm", Locale.getDefault())
private val dm = SimpleDateFormat("d MMM", Locale.getDefault())
private val dmhm = SimpleDateFormat("d MMM HH:mm", Locale.getDefault())

/** A tick on an earlier day needs its date: nothing else on screen says which day it is. */
private fun timeLabel(at: Long, spanMs: Long, todayStart: Long): String = when {
    spanMs > 2 * 24 * 3600_000L -> dm.format(Date(at))
    at < todayStart -> dmhm.format(Date(at))
    spanMs > 30 * 60_000L -> hm.format(Date(at))
    else -> hms.format(Date(at))
}

/**
 * Pinch to zoom, drag to pan; both move the window handed to Rust rather than
 * scaling a bitmap, so zooming in fetches finer data instead of magnifying what
 * was already reduced.
 */
@Composable
fun ValueChart(
    points: List<ChartPoint>,
    bands: List<Band>,
    window: LongRange,
    onWindowChange: (LongRange) -> Unit,
    /// Fixed vertical range for the series, in its own units. A scale that
    /// refits itself to whatever is on screen makes the same reading look
    /// different depending on where you scrolled from.
    axis: ClosedFloatingPointRange<Double>,
    decimals: Int,
    grid: GridStyle = GridStyle.Time,
    /// How far the view may be panned. A finished workout is bounded at both
    /// ends by its own extent; an open-ended series only by the present.
    limit: LongRange? = null,
    modifier: Modifier = Modifier,
    height: Dp = 260.dp,
    lineColor: Color = Color(0xFF4C7EF3),
    gridColor: Color = Color(0x22000000),
    axisColor: Color = Color(0x66000000),
    labelColor: Color = Color(0x99000000),
) {
    val measurer = rememberTextMeasurer()
    // Read inside the gesture without keying the handler on it: keying on the
    // window restarts pointerInput on the first emission, which cancels the
    // pinch in progress and limits a gesture to a single step.
    val latest = rememberUpdatedState(window)
    val bounds = rememberUpdatedState(limit)
    val gutterLeftDp = 34.dp
    val todayStart = todayStartMs()

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

            val (lo, hi) = bounds(points.filter { it.atMs in window }, axis, grid)
            val spanMs = (window.last - window.first).coerceAtLeast(1L)

            fun x(at: Long) = gutterLeft + ((at - window.first).toFloat() / spanMs) * plotWidth
            fun y(value: Double) =
                plotHeight - ((value - lo) / (hi - lo)).toFloat() * plotHeight

            for (band in bands) {
                val from = x(band.fromMs)
                val to = band.toMs?.let { x(it) } ?: size.width
                drawRect(
                    band.color,
                    Offset(from, 0f),
                    Size((to - from).coerceAtLeast(1f), plotHeight),
                )
            }

            val step = when (grid) {
                GridStyle.Time -> niceStep(hi - lo)
                GridStyle.EcgPaper -> ECG_MV_PER_LARGE_SQUARE
            }
            if (grid == GridStyle.EcgPaper) {
                // Minor ruling carries no labels; it is there to be counted.
                var minor = ceil(lo / ECG_MV_PER_SMALL_SQUARE) * ECG_MV_PER_SMALL_SQUARE
                while (minor <= hi) {
                    val at = y(minor)
                    drawLine(
                        gridColor.copy(alpha = 0.35f),
                        Offset(gutterLeft, at),
                        Offset(size.width, at),
                        hairline,
                    )
                    minor += ECG_MV_PER_SMALL_SQUARE
                }
            }
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
            val tickMs = when (grid) {
                GridStyle.EcgPaper -> ECG_MS_PER_LARGE_SQUARE
                GridStyle.Time ->
                    (TICK_SECONDS.firstOrNull { spanMs / 1000 / it <= 6 }
                        ?: TICK_SECONDS.last()) * 1000L
            }
            if (grid == GridStyle.EcgPaper) {
                var minor = (window.first / ECG_MS_PER_SMALL_SQUARE) * ECG_MS_PER_SMALL_SQUARE
                while (minor <= window.last) {
                    val at = x(minor)
                    drawLine(
                        gridColor.copy(alpha = 0.35f),
                        Offset(at, 0f),
                        Offset(at, plotHeight),
                        hairline,
                    )
                    minor += ECG_MS_PER_SMALL_SQUARE
                }
            }
            // Only an ECG labels a subset of its ruling: it counts whole
            // seconds from the start of the recording.
            val origin = limit?.first ?: window.first
            var tick = (window.first / tickMs) * tickMs
            if (tick < window.first) tick += tickMs
            while (tick <= window.last) {
                val at = x(tick)
                drawLine(gridColor, Offset(at, 0f), Offset(at, plotHeight), hairline)
                if (grid == GridStyle.EcgPaper && (tick - origin) % 1_000L != 0L) {
                    tick += tickMs
                    continue
                }
                val text = if (grid == GridStyle.EcgPaper) {
                    "${(tick - origin) / 1000}s"
                } else {
                    timeLabel(tick, spanMs, todayStart)
                }
                val label = measurer.measure(
                    text,
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
    grid: GridStyle,
): Pair<Double, Double> {
    // Ruled paper means fixed squares. Growing the axis to swallow an outlier
    // would leave a grid that no longer measures anything.
    if (grid == GridStyle.EcgPaper) return axis.start to axis.endInclusive
    if (points.isEmpty()) return axis.start to axis.endInclusive
    val dataLo = points.minOf { it.value }
    val dataHi = points.maxOf { it.value }
    // Rounding applies to the widening only: a series that asks for 50..150 is
    // stating where its readings live, and snapping that to 40..160 throws the
    // choice away.
    val step = niceStep(maxOf(dataHi, axis.endInclusive) - minOf(dataLo, axis.start))
    val lo = if (dataLo < axis.start) floor(dataLo / step) * step else axis.start
    val hi = if (dataHi > axis.endInclusive) ceil(dataHi / step) * step else axis.endInclusive
    return lo to hi
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
