package dev.davidv.withoutings.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.gestures.calculateCentroid
import androidx.compose.foundation.gestures.calculatePan
import androidx.compose.foundation.gestures.calculateZoom
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.DrawScope
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
import dev.davidv.withoutings.ui.theme.AppTheme
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.math.abs
import kotlin.math.ceil
import kotlin.math.floor

/**
 * How the background is ruled.
 *
 * A trend line wants readable round numbers wherever the axis happens to land.
 * An ECG wants the paper it has always been printed on — 25 mm/s and 10 mm/mV,
 * so a large square is 0.2 s by 0.5 mV — because that is what makes an interval
 * measurable by eye.
 */
enum class GridStyle { Time, EcgPaper }

/**
 * A horizontal stripe across the plot, in the series' own units.
 *
 * This is how the design states a fact about the person rather than about the
 * clock: the resting range is a band at 50–68 bpm, and a heart-rate zone is a
 * band from its floor to the top of the axis. Both are true wherever the window
 * is scrolled to, which is the point.
 */
data class ValueBand(
    val from: Double,
    val to: Double,
    val alpha: Float,
)

/** A dashed line marking one value, such as the resting rate itself. */
data class Guide(val value: Double, val label: String? = null)

/**
 * A stretch of the window a recorded session covers.
 *
 * Time is explained by attribution rather than by expectation: sessions shade
 * the window they cover, so two workouts are simply two blocks and a spike
 * outside any session is left unshaded rather than treated as anomalous.
 */
data class ChartSession(val span: Span, val label: String)

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

/**
 * Round wall-clock instants to rule a span at, coarse enough to leave about six
 * of them. Shared so that charts stacked on one screen tick together.
 */
fun timeTicks(window: LongRange): List<Long> {
    val spanMs = (window.last - window.first).coerceAtLeast(1L)
    val tickMs = tickIntervalMs(spanMs)
    val out = mutableListOf<Long>()
    var tick = (window.first / tickMs) * tickMs
    if (tick < window.first) tick += tickMs
    while (tick <= window.last) {
        out.add(tick)
        tick += tickMs
    }
    return out
}

private fun tickIntervalMs(spanMs: Long): Long =
    (TICK_SECONDS.firstOrNull { spanMs / 1000 / it <= 6 } ?: TICK_SECONDS.last()) * 1000L

/** As [timeTicks] labels them, for a chart drawing its own axis. */
fun timeTickLabel(at: Long, window: LongRange): String =
    timeLabel(at, (window.last - window.first).coerceAtLeast(1L), dayStart(System.currentTimeMillis()))

/** A tick on an earlier day needs its date: nothing else on screen says which day it is. */
private fun timeLabel(at: Long, spanMs: Long, todayStart: Long): String = when {
    spanMs > 2 * 24 * 3600_000L -> dm.format(Date(at))
    at < todayStart -> dmhm.format(Date(at))
    spanMs > 30 * 60_000L -> hm.format(Date(at))
    else -> hms.format(Date(at))
}

/**
 * The series, and everything drawn behind and over it.
 *
 * Pinch to zoom, drag to scrub. Both the window and the cursor are hoisted, so
 * two charts on one screen can be handed the same pair and move together —
 * which is what the design means by "where two charts share a screen they share
 * the cursor".
 */
@Composable
fun ValueChart(
    points: List<ChartPoint>,
    window: LongRange,
    axis: ClosedFloatingPointRange<Double>,
    decimals: Int,
    modifier: Modifier = Modifier,
    /// Null hands the decision to [modifier], for a chart told to absorb
    /// whatever height the screen has left.
    height: Dp? = 150.dp,
    onWindowChange: ((LongRange) -> Unit)? = null,
    /// Where the cursor sits, or null for a chart nobody has touched yet.
    scrubAtMs: Long? = null,
    onScrub: ((Long?) -> Unit)? = null,
    sessions: List<ChartSession> = emptyList(),
    /// Names the shaded blocks in a strip under the plot.
    labelSessions: Boolean = false,
    bands: List<ValueBand> = emptyList(),
    guides: List<Guide> = emptyList(),
    grid: GridStyle = GridStyle.Time,
    /// How far the view may be panned. A finished session is bounded at both
    /// ends by its own extent; an open-ended series only by the present.
    limit: LongRange? = null,
    showTimeAxis: Boolean = true,
    /// A second chart sharing a cursor draws it fainter, so one of them still
    /// reads as the one being touched.
    cursorAlpha: Float = 1f,
    unit: String = "",
    lineColor: Color = AppTheme.colors.dataStroke,
    fillColor: Color = MaterialTheme.colorScheme.primary,
) {
    val measurer = rememberTextMeasurer()
    val tokens = AppTheme.chart
    val colors = AppTheme.colors
    val scheme = MaterialTheme.colorScheme
    val axisStyle = AppTheme.type.axis.copy(color = colors.onSurfaceDim)
    val sessionStyle = AppTheme.type.axisSmall.copy(color = scheme.onSurfaceVariant)
    val tooltipStyle = AppTheme.type.tooltip.copy(color = scheme.surface)
    val ecgAxisStyle = AppTheme.type.axis.copy(color = colors.ecgMeta)

    // Read inside the gesture without keying the handler on it: keying on the
    // window restarts pointerInput on the first emission, which cancels the
    // pinch in progress and limits a gesture to a single step.
    val latest = rememberUpdatedState(window)
    val bounds = rememberUpdatedState(limit)
    val todayStart = dayStart(System.currentTimeMillis())

    Box(
        modifier
            .fillMaxWidth()
            .then(if (height != null) Modifier.height(height) else Modifier)
            .pointerInput(Unit) {
                awaitEachGesture {
                    val down = awaitFirstDown(requireUnconsumed = false)
                    // The window a gesture works from is its own, so a slow
                    // recomposition cannot feed a stale value back in.
                    var live = latest.value
                    var pointers = 1
                    // Landing the cursor on the first touch: a drag that only
                    // takes effect once it has moved makes a tap look ignored.
                    onScrub?.invoke(atX(down.position.x, size.width, live))
                    do {
                        val event = awaitPointerEvent()
                        pointers = maxOf(pointers, event.changes.count { it.pressed })
                        val zoom = event.calculateZoom()
                        val pan = event.calculatePan()

                        // One finger scrubs, two zoom. A single-pointer drag
                        // that also panned would fight the cursor for the same
                        // gesture, and the cursor is what the screen is read
                        // with — so only a chart with no cursor lets one
                        // finger pan it instead.
                        if (pointers < 2 && onScrub != null) {
                            val x = event.changes.firstOrNull()?.position?.x ?: continue
                            onScrub(atX(x, size.width, live))
                            event.changes.forEach { it.consume() }
                            continue
                        }
                        if (zoom == 1f && pan.x == 0f) continue
                        if (onWindowChange == null) continue

                        val plotWidth = size.width.toFloat().coerceAtLeast(1f)
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
                        val anchor = (event.calculateCentroid(useCurrent = true).x / plotWidth)
                            .coerceIn(0f, 1f)
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
        Canvas(Modifier.fillMaxSize()) {
            val axisHeight = if (showTimeAxis) 16.dp.toPx() else 0f
            val labelStrip = if (labelSessions) 13.dp.toPx() else 0f
            val plotHeight = size.height - axisHeight - labelStrip
            if (size.width <= 0 || plotHeight <= 0) return@Canvas

            val visible = points.filter { it.atMs in window }
            val (lo, hi) = valueBounds(visible, axis, grid)
            val spanMs = (window.last - window.first).coerceAtLeast(1L)

            fun x(at: Long) = ((at - window.first).toFloat() / spanMs) * size.width
            fun y(value: Double) =
                plotHeight - ((value - lo) / (hi - lo)).toFloat() * plotHeight

            // Bottom up: session shading, then value bands, then the ruling,
            // then the trace, then the cursor. Anything that answers "what was
            // happening" sits behind anything that answers "what was measured".
            for (session in sessions) {
                val from = x(session.span.fromMs).coerceAtLeast(0f)
                val to = x(session.span.toMs).coerceAtMost(size.width)
                if (to <= from) continue
                drawRect(
                    fillColor.copy(alpha = tokens.sessionAlpha),
                    Offset(from, 0f),
                    Size(to - from, plotHeight),
                )
            }

            for (band in bands) {
                val top = y(band.to.coerceAtMost(hi)).coerceAtLeast(0f)
                val bottom = y(band.from.coerceAtLeast(lo)).coerceAtMost(plotHeight)
                if (bottom <= top) continue
                drawRect(
                    fillColor.copy(alpha = band.alpha),
                    Offset(0f, top),
                    Size(size.width, bottom - top),
                )
            }

            if (grid == GridStyle.EcgPaper) {
                drawEcgPaper(colors.ecgGrid, tokens.ecgMinorAlpha, window, lo, hi, ::x, ::y, plotHeight)
            } else {
                val step = niceStep(hi - lo)
                var value = ceil(lo / step) * step
                while (value <= hi + step / 1000) {
                    val at = y(value)
                    drawLine(
                        colors.chartGrid,
                        Offset(0f, at),
                        Offset(size.width, at),
                        tokens.grid.toPx(),
                    )
                    value += step
                }
            }

            for (guide in guides) {
                if (guide.value < lo || guide.value > hi) continue
                val at = y(guide.value)
                drawLine(
                    fillColor,
                    Offset(0f, at),
                    Offset(size.width, at),
                    tokens.cursor.toPx(),
                    pathEffect = PathEffect.dashPathEffect(
                        floatArrayOf(4.dp.toPx(), 4.dp.toPx()),
                    ),
                )
            }

            if (visible.isNotEmpty() || points.isNotEmpty()) {
                drawTrace(points, ::x, ::y, plotHeight, lineColor, fillColor, tokens)
            }

            if (showTimeAxis) {
                val tickMs = when (grid) {
                    GridStyle.EcgPaper -> ECG_MS_PER_LARGE_SQUARE
                    GridStyle.Time -> tickIntervalMs(spanMs)
                }
                val origin = limit?.first ?: window.first
                val style = if (grid == GridStyle.EcgPaper) ecgAxisStyle else axisStyle
                var tick = (window.first / tickMs) * tickMs
                if (tick < window.first) tick += tickMs
                while (tick <= window.last) {
                    // Only an ECG labels a subset of its ruling: it counts
                    // whole seconds from the start of the recording.
                    if (grid == GridStyle.EcgPaper && (tick - origin) % 1_000L != 0L) {
                        tick += tickMs
                        continue
                    }
                    val text = if (grid == GridStyle.EcgPaper) {
                        "${(tick - origin) / 1000} s"
                    } else {
                        timeLabel(tick, spanMs, todayStart)
                    }
                    val label = measurer.measure(text, style)
                    val left = (x(tick) - label.size.width / 2)
                        .coerceIn(0f, (size.width - label.size.width).coerceAtLeast(0f))
                    drawText(
                        label,
                        topLeft = Offset(left, plotHeight + labelStrip + 2.dp.toPx()),
                    )
                    tick += tickMs
                }
            }

            if (labelSessions) {
                for (session in sessions) {
                    val from = x(session.span.fromMs).coerceAtLeast(0f)
                    val to = x(session.span.toMs).coerceAtMost(size.width)
                    if (to <= from) continue
                    val label = measurer.measure(session.label, sessionStyle)
                    if (label.size.width > to - from) continue
                    drawText(
                        label,
                        topLeft = Offset(
                            from + (to - from - label.size.width) / 2,
                            plotHeight + 1.dp.toPx(),
                        ),
                    )
                }
            }

            val cursor = scrubAtMs?.takeIf { it in window }
            if (cursor != null) {
                val nearest = points.minByOrNull { abs(it.atMs - cursor) }
                val at = x(nearest?.atMs ?: cursor)
                drawLine(
                    scheme.onSurface.copy(alpha = cursorAlpha),
                    Offset(at, 0f),
                    Offset(at, plotHeight),
                    tokens.cursor.toPx(),
                )
                if (nearest != null && nearest.value in lo..hi) {
                    drawCircle(
                        scheme.onSurface.copy(alpha = cursorAlpha),
                        radius = tokens.cursorDot.toPx(),
                        center = Offset(at, y(nearest.value)),
                    )
                    if (cursorAlpha > 0.9f) {
                        drawTooltip(
                            measurer,
                            tooltipStyle,
                            scheme.onSurface,
                            "${formatValue(nearest.value, decimals)}$unit · ${clock(nearest.atMs)}",
                            at,
                            size.width,
                        )
                    }
                }
            }
        }
    }
}

/** The instant under a touch, so a drag scrubs in the series' own units. */
private fun atX(x: Float, width: Int, window: LongRange): Long {
    val fraction = (x / width.toFloat().coerceAtLeast(1f)).coerceIn(0f, 1f)
    return window.first + ((window.last - window.first) * fraction).toLong()
}

private fun DrawScope.drawTrace(
    points: List<ChartPoint>,
    x: (Long) -> Float,
    y: (Double) -> Float,
    plotHeight: Float,
    lineColor: Color,
    fillColor: Color,
    tokens: dev.davidv.withoutings.ui.theme.ChartTokens,
) {
    if (points.isEmpty()) return
    val ordered = points.sortedBy { it.atMs }
    val path = Path()
    ordered.forEachIndexed { index, point ->
        val px = x(point.atMs)
        val py = y(point.value)
        if (index == 0) path.moveTo(px, py) else path.lineTo(px, py)
    }

    // The series deliberately reaches one point beyond each edge so the line
    // arrives from off-screen instead of stopping at the plot; the clip is what
    // keeps that off the axis and the labels.
    clipRect(left = 0f, top = 0f, right = size.width, bottom = plotHeight) {
        val fill = Path().apply {
            addPath(path)
            lineTo(x(ordered.last().atMs), plotHeight)
            lineTo(x(ordered.first().atMs), plotHeight)
            close()
        }
        drawPath(fill, fillColor.copy(alpha = tokens.areaAlpha), style = Fill)
        drawPath(
            path,
            lineColor,
            style = Stroke(
                width = tokens.trace.toPx(),
                cap = StrokeCap.Round,
                join = StrokeJoin.Round,
            ),
        )

        // A daily total gives one point per day; a line between two of them is
        // invisible and a single one draws nothing at all.
        if (ordered.size <= 60) {
            ordered.forEach {
                drawCircle(
                    lineColor,
                    radius = tokens.trace.toPx(),
                    center = Offset(x(it.atMs), y(it.value)),
                )
            }
        }
    }
}

/** The dark pill that reads out the sample under the cursor. */
private fun DrawScope.drawTooltip(
    measurer: TextMeasurer,
    style: TextStyle,
    container: Color,
    text: String,
    at: Float,
    width: Float,
) {
    val label = measurer.measure(text, style)
    val padH = 9.dp.toPx()
    val padV = 5.dp.toPx()
    val boxWidth = label.size.width + padH * 2
    val boxHeight = label.size.height + padV * 2
    val left = (at - boxWidth / 2).coerceIn(0f, (width - boxWidth).coerceAtLeast(0f))
    drawRoundRect(
        container,
        topLeft = Offset(left, 0f),
        size = Size(boxWidth, boxHeight),
        cornerRadius = CornerRadius(9.dp.toPx(), 9.dp.toPx()),
    )
    drawText(label, topLeft = Offset(left + padH, padV))
}

/**
 * Ruled paper, 1 mm and 5 mm.
 *
 * The ratio is the whole point: the strip has to be readable as a real rhythm
 * strip, which means counting squares must measure an interval.
 */
private fun DrawScope.drawEcgPaper(
    gridColor: Color,
    minorAlpha: Float,
    window: LongRange,
    lo: Double,
    hi: Double,
    x: (Long) -> Float,
    y: (Double) -> Float,
    plotHeight: Float,
) {
    val fine = 0.4.dp.toPx()
    val bold = 0.8.dp.toPx()

    var minor = ceil(lo / ECG_MV_PER_SMALL_SQUARE) * ECG_MV_PER_SMALL_SQUARE
    while (minor <= hi) {
        val at = y(minor)
        drawLine(gridColor.copy(alpha = minorAlpha), Offset(0f, at), Offset(size.width, at), fine)
        minor += ECG_MV_PER_SMALL_SQUARE
    }
    var major = ceil(lo / ECG_MV_PER_LARGE_SQUARE) * ECG_MV_PER_LARGE_SQUARE
    while (major <= hi) {
        val at = y(major)
        drawLine(gridColor, Offset(0f, at), Offset(size.width, at), bold)
        major += ECG_MV_PER_LARGE_SQUARE
    }

    var minorX = (window.first / ECG_MS_PER_SMALL_SQUARE) * ECG_MS_PER_SMALL_SQUARE
    while (minorX <= window.last) {
        val at = x(minorX)
        drawLine(gridColor.copy(alpha = minorAlpha), Offset(at, 0f), Offset(at, plotHeight), fine)
        minorX += ECG_MS_PER_SMALL_SQUARE
    }
    var majorX = (window.first / ECG_MS_PER_LARGE_SQUARE) * ECG_MS_PER_LARGE_SQUARE
    while (majorX <= window.last) {
        val at = x(majorX)
        drawLine(gridColor, Offset(at, 0f), Offset(at, plotHeight), bold)
        majorX += ECG_MS_PER_LARGE_SQUARE
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
private fun valueBounds(
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

fun formatValue(value: Double, decimals: Int): String =
    if (decimals == 0) value.toInt().toString()
    else String.format(Locale.US, "%.${decimals}f", value)
