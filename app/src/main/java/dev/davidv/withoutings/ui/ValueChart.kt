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

enum class GridStyle { Time, EcgPaper }

data class Guide(val value: Double, val label: String? = null)

data class ChartSession(val span: Span, val label: String)

private val TICK_SECONDS = listOf(
    10, 30, 60, 120, 300, 600, 1800, 3600, 7200, 21600, 43200,
    86400, 2 * 86400, 7 * 86400, 30 * 86400,
)

private const val ECG_MS_PER_LARGE_SQUARE = 200L
private const val ECG_MS_PER_SMALL_SQUARE = 40L
private const val ECG_MV_PER_LARGE_SQUARE = 0.5
private const val ECG_MV_PER_SMALL_SQUARE = 0.1

private val NICE_STEPS = listOf(1.0, 2.0, 5.0)

private val AXIS_STRIP = 16.dp
private val SESSION_LABEL_STRIP = 13.dp
private val PAN_HANDLE_MIN = 24.dp

private enum class ChartGesture { Scrub, Pan }

private val hms = SimpleDateFormat("HH:mm:ss", Locale.getDefault())
private val hm = SimpleDateFormat("HH:mm", Locale.getDefault())
private val dm = SimpleDateFormat("d MMM", Locale.getDefault())
private val dmhm = SimpleDateFormat("d MMM HH:mm", Locale.getDefault())

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

fun timeTickLabel(at: Long, window: LongRange): String =
    timeLabel(at, (window.last - window.first).coerceAtLeast(1L), dayStart(System.currentTimeMillis()))

private fun timeLabel(at: Long, spanMs: Long, todayStart: Long): String = when {
    spanMs > 2 * 24 * 3600_000L -> dm.format(Date(at))
    at < todayStart -> dmhm.format(Date(at))
    spanMs > 30 * 60_000L -> hm.format(Date(at))
    else -> hms.format(Date(at))
}

@Composable
fun ValueChart(
    points: List<ChartPoint>,
    window: LongRange,
    axis: ClosedFloatingPointRange<Double>,
    decimals: Int,
    modifier: Modifier = Modifier,
    height: Dp? = 150.dp,
    onWindowChange: ((LongRange) -> Unit)? = null,
    scrubAtMs: Long? = null,
    onScrub: ((Long?) -> Unit)? = null,
    sessions: List<ChartSession> = emptyList(),
    labelSessions: Boolean = false,
    guides: List<Guide> = emptyList(),
    grid: GridStyle = GridStyle.Time,
    limit: LongRange? = null,
    showTimeAxis: Boolean = true,
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

    val latest = rememberUpdatedState(window)
    val bounds = rememberUpdatedState(limit)
    val todayStart = dayStart(System.currentTimeMillis())

    val axisStrip = if (showTimeAxis) AXIS_STRIP else 0.dp
    val labelStripDp = if (labelSessions) SESSION_LABEL_STRIP else 0.dp
    val panHandle = maxOf(axisStrip + labelStripDp, PAN_HANDLE_MIN)

    Box(
        modifier
            .fillMaxWidth()
            .then(if (height != null) Modifier.height(height) else Modifier)
            .pointerInput(panHandle) {
                awaitEachGesture {
                    val down = awaitFirstDown(requireUnconsumed = false)
                    var live = latest.value
                    var pointers = 1
                    val mode = if (
                        onWindowChange != null && down.position.y >= size.height - panHandle.toPx()
                    ) ChartGesture.Pan else ChartGesture.Scrub
                    if (mode == ChartGesture.Scrub) {
                        onScrub?.invoke(atX(down.position.x, size.width, live))
                    }
                    do {
                        val event = awaitPointerEvent()
                        pointers = maxOf(pointers, event.changes.count { it.pressed })
                        val zoom = event.calculateZoom()
                        val pan = event.calculatePan()

                        if (pointers < 2 && mode == ChartGesture.Scrub && onScrub != null) {
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
                        var scaled = (span / zoom).toLong()
                            .coerceIn(10_000L, 365L * 24 * 3600 * 1000)
                        if (oldest != null) scaled = scaled.coerceAtMost(newest - oldest)

                        val anchor = (event.calculateCentroid(useCurrent = true).x / plotWidth)
                            .coerceIn(0f, 1f)
                        val at = live.first + (span * anchor).toLong()
                        val shift = -(pan.x / plotWidth * scaled).toLong()
                        var first = at - (scaled * anchor).toLong() + shift

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
            val axisHeight = axisStrip.toPx()
            val labelStrip = labelStripDp.toPx()
            val plotHeight = size.height - axisHeight - labelStrip
            if (size.width <= 0 || plotHeight <= 0) return@Canvas

            val nearby = points.spanning(window)
            val visible = nearby.filter { it.atMs in window }
            val (lo, hi) = valueBounds(visible, axis, grid)
            val spanMs = (window.last - window.first).coerceAtLeast(1L)

            fun x(at: Long) = ((at - window.first).toFloat() / spanMs) * size.width
            fun y(value: Double) =
                plotHeight - ((value - lo) / (hi - lo)).toFloat() * plotHeight

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

            drawTrace(nearby, ::x, ::y, plotHeight, lineColor, fillColor, tokens)

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
                val nearest = nearby.minByOrNull { abs(it.atMs - cursor) }
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

private fun valueBounds(
    points: List<ChartPoint>,
    axis: ClosedFloatingPointRange<Double>,
    grid: GridStyle,
): Pair<Double, Double> {
    if (grid == GridStyle.EcgPaper) return axis.start to axis.endInclusive
    if (points.isEmpty()) return axis.start to axis.endInclusive
    val dataLo = points.minOf { it.value }
    val dataHi = points.maxOf { it.value }
    val step = niceStep(maxOf(dataHi, axis.endInclusive) - minOf(dataLo, axis.start))
    val lo = if (dataLo < axis.start) floor(dataLo / step) * step else axis.start
    val hi = if (dataHi > axis.endInclusive) ceil(dataHi / step) * step else axis.endInclusive
    return lo to hi
}

private fun niceStep(span: Double): Double {
    if (span <= 0) return 1.0
    val magnitude = Math.pow(10.0, floor(kotlin.math.log10(span / 6)))
    return NICE_STEPS.map { it * magnitude }.firstOrNull { span / it <= 6 }
        ?: (10 * magnitude)
}

fun formatValue(value: Double, decimals: Int): String =
    if (decimals == 0) value.toInt().toString()
    else String.format(Locale.US, "%.${decimals}f", value)
