package dev.davidv.withoutings.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.gestures.detectTransformGestures
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.CloseFullscreen
import androidx.compose.material.icons.rounded.OpenInFull
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.lerp
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.text.TextMeasurer
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.drawText
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.Alignment
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import dev.davidv.withoutings.ui.theme.AppTheme
import kotlin.math.ceil
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlin.math.roundToInt
import uniffi.wpp_ffi.MapFrame
import uniffi.wpp_ffi.MapPoint
import uniffi.wpp_ffi.MapTile
import uniffi.wpp_ffi.MapView
import uniffi.wpp_ffi.RouteTrack
import uniffi.wpp_ffi.TrackSummary
import uniffi.wpp_ffi.panMap
import uniffi.wpp_ffi.zoomMap

private val MAP_HEIGHT = 340.dp
private const val FIT_PADDING_PX = 24.0
private const val FIRST_RETRY_MS = 1_500L
private const val LAST_RETRY_MS = 20_000L

/**
 * The route, over tiles when there are any and over nothing when there are
 * not. Every question about where a point lands is answered on the other side
 * of the boundary: this draws what it is handed and turns gestures back into
 * a view, which is the whole of what it knows about maps.
 */
@Composable
fun TrackMap(route: Route, tiles: TileSource?, cursorAtMs: Long?, onExpand: () -> Unit) {
    Box {
        // Inert in the card: a map that pans would swallow every attempt to
        // scroll the page past it, and half a drag would leave it looking at
        // somewhere the session never went. Reading it happens expanded.
        MapCanvas(route, tiles, cursorAtMs, MAP_HEIGHT, panning = false, onTap = onExpand)
        MapCorner(Icons.Rounded.OpenInFull, "Fill the page with the route", onExpand)
    }
}

/**
 * The route given the whole page under the session's own heading, which is as
 * much as it can have without hiding what it belongs to.
 */
@Composable
fun ExpandedTrackMap(
    route: Route,
    tiles: TileSource?,
    cursorAtMs: Long?,
    onShrink: () -> Unit,
) {
    Box(Modifier.fillMaxSize()) {
        MapCanvas(route, tiles, cursorAtMs, height = null, panning = true, onTap = {})
        MapCorner(Icons.Rounded.CloseFullscreen, "Back to the session", onShrink)
    }
}

@Composable
private fun BoxScope.MapCorner(
    icon: androidx.compose.ui.graphics.vector.ImageVector,
    description: String,
    onClick: () -> Unit,
) {
    Box(
        Modifier
            .align(Alignment.BottomEnd)
            .padding(6.dp)
            .clip(CircleShape)
            .background(MaterialTheme.colorScheme.surface.copy(alpha = 0.85f)),
    ) {
        GlyphButton(icon, description, size = 18.dp, onClick = onClick)
    }
}

@Composable
private fun MapCanvas(
    route: Route,
    tiles: TileSource?,
    cursorAtMs: Long?,
    height: Dp?,
    panning: Boolean,
    onTap: () -> Unit,
) {
    val track = route.track
    var size by remember(track) { mutableStateOf(IntSize.Zero) }
    var fitted by remember(track) { mutableStateOf(IntSize.Zero) }
    var view by remember(track) { mutableStateOf<MapView?>(null) }
    val loaded = remember(track, tiles) { mutableStateMapOf<String, ImageBitmap>() }
    val measurer = rememberTextMeasurer()
    val line = MaterialTheme.colorScheme.primary
    val ground = MaterialTheme.colorScheme.surfaceContainer
    val meta = AppTheme.colors.onSurfaceTertiary
    val strokePx = with(LocalDensity.current) { 3.dp.toPx() }
    val dotPx = with(LocalDensity.current) { 4.dp.toPx() }

    val held = view
    val frame: MapFrame? = remember(track, held) { held?.let { track.frame(it) } }

    if (tiles != null && frame != null) {
        // A tile the server refused once is asked for again: without this a
        // single failure leaves a grey square there until the map is moved.
        LaunchedEffect(frame.tiles) {
            var wait = FIRST_RETRY_MS
            while (true) {
                coroutineScope {
                    for (tile in frame.tiles) {
                        val key = "${tile.z}/${tile.x}/${tile.y}"
                        if (loaded.containsKey(key)) continue
                        launch { tiles.fetch(tile)?.let { loaded[key] = it } }
                    }
                }
                val missing = frame.tiles.any { !loaded.containsKey("${it.z}/${it.x}/${it.y}") }
                if (!missing || wait > LAST_RETRY_MS) return@LaunchedEffect
                delay(wait)
                wait *= 2
            }
        }
    }

    val refit = { measured: IntSize ->
        track.fit(measured.width.toDouble(), measured.height.toDouble(), FIT_PADDING_PX)
    }

    Canvas(
        Modifier
            .fillMaxWidth()
            .then(if (height != null) Modifier.height(height) else Modifier.fillMaxSize())
            .clip(RoundedCornerShape(AppTheme.radius.card))
            .onSizeChanged { measured ->
                if (measured.width == 0 || measured.height == 0) return@onSizeChanged
                size = measured
                // Fitted against the box it is actually in. An early pass can
                // measure a sliver, and a route fitted to that sits at a zoom
                // showing half the county once the real height arrives.
                if (measured == fitted) return@onSizeChanged
                fitted = measured
                view = refit(measured)
            }
            .then(
                if (!panning) {
                    Modifier.pointerInput(track) { detectTapGestures(onTap = { onTap() }) }
                } else {
                    Modifier
                        .pointerInput(track) {
                            detectTransformGestures { centroid, panned, zoomed, _ ->
                                val current = view ?: return@detectTransformGestures
                                val closer = zoomMap(
                                    current,
                                    zoomed.toDouble(),
                                    centroid.x.toDouble(),
                                    centroid.y.toDouble(),
                                )
                                view = panMap(closer, panned.x.toDouble(), panned.y.toDouble())
                            }
                        }
                        .pointerInput(track) {
                            detectTapGestures(
                                onDoubleTap = { if (size.width > 0) view = refit(size) }
                            )
                        }
                }
            )
    ) {
        drawRect(ground)
        val drawn = frame ?: return@Canvas

        if (tiles != null) {
            for (tile in drawn.tiles) {
                drawTile(tile, loaded["${tile.z}/${tile.x}/${tile.y}"])
            }
        }
        drawRoute(drawn, route.summary, line, strokePx, dotPx)
        cursorAtMs?.let { at ->
            held?.let { track.cursor(it, at) }?.let { drawCursor(it, line, dotPx) }
        }
        drawScaleBar(drawn.metresPerPixel, meta, measurer)
        if (tiles != null) {
            drawCredit(measurer, meta)
        }
    }
}

private fun DrawScope.drawTile(tile: MapTile, bitmap: ImageBitmap?) {
    if (bitmap == null) return
    // A tile drawn at a fractional zoom lands on fractional pixels; rounding
    // the size up rather than down keeps the seam between two of them covered.
    drawImage(
        image = bitmap,
        srcOffset = IntOffset.Zero,
        srcSize = IntSize(bitmap.width, bitmap.height),
        dstOffset = IntOffset(tile.leftPx.roundToInt(), tile.topPx.roundToInt()),
        dstSize = IntSize(ceil(tile.sizePx).toInt(), ceil(tile.sizePx).toInt()),
    )
}

private fun DrawScope.drawRoute(
    frame: MapFrame,
    summary: TrackSummary,
    colour: Color,
    strokePx: Float,
    dotPx: Float,
) {
    val slow = summary.slowMS
    val fast = summary.fastMS
    for (run in frame.path) {
        if (run.points.size < 2) continue
        // Leg by leg rather than one path: each carries the speed it was
        // covered at, which is the whole point of colouring it.
        if (slow == null || fast == null || fast <= slow) {
            val path = Path()
            path.moveTo(run.points[0].xPx.toFloat(), run.points[0].yPx.toFloat())
            for (point in run.points.drop(1)) {
                path.lineTo(point.xPx.toFloat(), point.yPx.toFloat())
            }
            drawPath(
                path,
                colour,
                style = Stroke(width = strokePx, cap = StrokeCap.Round, join = StrokeJoin.Round),
            )
            continue
        }
        for (leg in run.points.windowed(2)) {
            val speed = leg[1].speedMS ?: leg[0].speedMS
            drawLine(
                speedColour(speed, slow, fast, colour),
                Offset(leg[0].xPx.toFloat(), leg[0].yPx.toFloat()),
                Offset(leg[1].xPx.toFloat(), leg[1].yPx.toFloat()),
                strokeWidth = strokePx,
                cap = StrokeCap.Round,
            )
        }
    }

    val first = frame.path.firstOrNull()?.points?.firstOrNull()
    val last = frame.path.lastOrNull()?.points?.lastOrNull()
    if (first != null) {
        drawCircle(Color.White, dotPx + 1.5f, Offset(first.xPx.toFloat(), first.yPx.toFloat()))
        drawCircle(colour, dotPx, Offset(first.xPx.toFloat(), first.yPx.toFloat()))
    }
    if (last != null) {
        drawCircle(colour, dotPx + 2f, Offset(last.xPx.toFloat(), last.yPx.toFloat()))
        drawCircle(Color.White, dotPx - 1f, Offset(last.xPx.toFloat(), last.yPx.toFloat()))
    }
}

/**
 * Where the body was at the instant the charts are scrubbed to, placed against
 * the route's own fixes rather than the line drawn from them.
 */
private fun DrawScope.drawCursor(point: MapPoint, colour: Color, dotPx: Float) {
    val at = Offset(point.xPx.toFloat(), point.yPx.toFloat())
    drawCircle(Color.White, dotPx + 3f, at)
    drawCircle(colour, dotPx + 1f, at)
}

private fun DrawScope.drawScaleBar(metresPerPixel: Double, colour: Color, measurer: TextMeasurer) {
    val label = roundLength(metresPerPixel * (size.width / 4.0))
    val bar = (label / metresPerPixel).toFloat()
    if (bar <= 0f || bar > size.width) return

    val left = 10f
    val bottom = size.height - 12f
    drawLine(colour, Offset(left, bottom), Offset(left + bar, bottom), strokeWidth = 2f)
    drawLine(colour, Offset(left, bottom - 4f), Offset(left, bottom + 4f), strokeWidth = 2f)
    drawLine(colour, Offset(left + bar, bottom - 4f), Offset(left + bar, bottom + 4f), strokeWidth = 2f)
    drawText(
        measurer,
        if (label >= 1000) "${(label / 1000).toInt()} km" else "${label.toInt()} m",
        topLeft = Offset(left, bottom - 26f),
        style = TextStyle(color = colour, fontSize = 10.sp),
    )
}

/** One, two or five times a power of ten, so the bar reads as a round number. */
private fun roundLength(metres: Double): Double {
    if (metres <= 0.0) return 0.0
    var step = 1.0
    while (step * 10 <= metres) step *= 10
    return when {
        step * 5 <= metres -> step * 5
        step * 2 <= metres -> step * 2
        else -> step
    }
}

private fun DrawScope.drawCredit(measurer: TextMeasurer, colour: Color) {
    val credit = "© OpenStreetMap contributors"
    val laid = measurer.measure(credit, TextStyle(fontSize = 9.sp))
    // Top right: the scale bar has the bottom left and the control that opens
    // the map full screen has the bottom right.
    drawText(
        measurer,
        credit,
        topLeft = Offset(size.width - laid.size.width - 12f, 8f),
        style = TextStyle(color = colour, fontSize = 9.sp),
    )
}

val SLOW_COLOUR = Color(0xFFC62828)
val MIDDLING_COLOUR = Color(0xFFF9A825)
val FAST_COLOUR = Color(0xFF2E7D32)

/**
 * Red where the going was slow through to green where it was quick, the way a
 * traffic map reads. Over the middle of this route's own spread rather than
 * any absolute speed: a walk and a descent are both worth reading.
 */
fun speedColour(speed: Double?, slow: Double, fast: Double, unknown: Color): Color {
    if (speed == null) return unknown
    val part = ((speed - slow) / (fast - slow)).coerceIn(0.0, 1.0).toFloat()
    return if (part < 0.5f) {
        lerp(SLOW_COLOUR, MIDDLING_COLOUR, part * 2f)
    } else {
        lerp(MIDDLING_COLOUR, FAST_COLOUR, (part - 0.5f) * 2f)
    }
}
