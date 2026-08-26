package dev.davidv.withoutings.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.ContentCut
import androidx.compose.material.icons.rounded.DeleteOutline
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TimeInput
import androidx.compose.material3.rememberTimePickerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.unit.dp
import dev.davidv.withoutings.ui.theme.AppTheme
import java.util.Calendar
import kotlin.math.abs
import uniffi.wpp_ffi.ActivityTotals
import uniffi.wpp_ffi.TrackSummary
import uniffi.wpp_ffi.Travel

// A minute without a fix is a break in the line rather than a straight leg
// across whatever was not recorded, matching what the route itself is cut on.
private const val ROUTE_GAP_MS = 30_000L

private const val MINUTE_MS = 60_000L

@Composable
fun ActivityDetailScreen(
    state: UiState,
    entry: ActivityEntry?,
    window: LongRange,
    nowMs: Long,
    totals: ActivityTotals?,
    route: Route?,
    tiles: TileSource?,
    onWindowChange: (LongRange) -> Unit,
    onDelete: (RecordedEntry) -> Unit,
    onTrim: (RecordedEntry, Long) -> Unit,
    onBack: () -> Unit,
) {
    var scrubAtMs by remember { mutableStateOf<Long?>(null) }
    var mapFilling by remember(entry) { mutableStateOf(false) }
    var asking by remember { mutableStateOf(false) }
    var trimming by remember { mutableStateOf(false) }

    // Stacked charts share one scrubber, so their readings belong in one column
    // down the titles rather than at whatever height each line happens to sit.
    fun readout(points: List<ChartPoint>, unit: String, decimals: Int, idle: String?): String? {
        val at = scrubAtMs?.let { valueAt(points, it) } ?: return idle
        return "${grouped(at.value, decimals)}$unit"
    }

    val hr = state.hr.map { ChartPoint(it.atMs, it.bpm.toDouble()) }
    val temperature = state.workoutTemp
    val sets = state.markers.workSpans(entry?.endedAtMs ?: nowMs)
    val extent = entry?.let { it.startedAtMs..(it.endedAtMs ?: nowMs) }

    // A series arrives padded with the sample either side of the window, so the
    // drawn line reaches the edges of the chart rather than starting inside it.
    // A figure about the session is a different question and must not count a
    // reading taken before it began: one minute of sitting still ahead of a
    // workout is enough to move a baseline and lose a tenth of the rise.
    fun inSession(points: List<ChartPoint>) =
        extent?.let { span -> points.filter { it.atMs in span } } ?: points

    val sessionHr = inSession(hr)
    val sessionTemp = inSession(temperature)

    DetailScaffold(
        title = entry?.name ?: "Activity",
        subtitle = entry?.let {
            "${dayName(it.startedAtMs, nowMs)} · ${clock(it.startedAtMs)}" +
                (it.endedAtMs?.let { end -> " – ${clock(end)}" } ?: " – now")
        },
        onBack = onBack,
        gap = AppTheme.space.blockMetric,
        trailing = {
            if (entry is RecordedEntry) {
                if (entry.endedAtMs != null) {
                    GlyphButton(Icons.Rounded.ContentCut, "Trim the end") { trimming = true }
                }
                GlyphButton(Icons.Rounded.DeleteOutline, "Delete this session") { asking = true }
            }
        },
    ) {
        if (entry == null) {
            EmptyNote("No session selected.")
            return@DetailScaffold
        }

        if (route != null && mapFilling) {
            ExpandedTrackMap(route, tiles, scrubAtMs) { mapFilling = false }
            return@DetailScaffold
        }

        Column(
            Modifier.fillMaxSize().verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(AppTheme.space.blockMetric),
        ) {

        val elapsedMs = (entry.endedAtMs ?: nowMs) - entry.startedAtMs
        if (route != null) {
            FigureRail(paceFigures(entry, route, elapsedMs))
            RowDivider(inset = 0.dp)
        }

        val effort = effortFigures(entry, totals, route)
        if (effort.isNotEmpty()) {
            FigureRail(effort)
            RowDivider(inset = 0.dp)
        }

        FigureRail(summaryFigures(sessionHr, sessionTemp))

        ChartTitle("Heart rate", readout(sessionHr, " bpm", 0, null))
        ChartCard {
            ValueChart(
                points = hr,
                window = window,
                axis = MetricStyle.HeartRate.axis,
                decimals = 0,
                height = 150.dp,
                onWindowChange = onWindowChange,
                scrubAtMs = scrubAtMs,
                onScrub = { scrubAtMs = it },
                sessions = sets.chartSessions("set"),
                limit = extent,
                unit = " bpm",
                readout = ChartReadout.Titled,
            )
            Spacer(Modifier.height(4.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                LegendSwatch(
                    MaterialTheme.colorScheme.primary.copy(alpha = AppTheme.chart.legendSessionAlpha),
                    "Set timed here",
                )
            }
        }

        if (route != null && route.speed.isNotEmpty()) {
            ChartTitle(
                "Speed",
                readout(
                    route.speed,
                    " km/h",
                    1,
                    "${grouped(route.speed.maxOf { it.value }, 1)} km/h at its fastest",
                ),
            )
            ChartCard {
                ValueChart(
                    points = route.speed,
                    window = window,
                    axis = 0.0..10.0,
                    decimals = 1,
                    height = 110.dp,
                    onWindowChange = onWindowChange,
                    scrubAtMs = scrubAtMs,
                    onScrub = { scrubAtMs = it },
                    limit = extent,
                    connectWithin = ROUTE_GAP_MS,
                    unit = " km/h",
                    readout = ChartReadout.Titled,
                )
            }
        }

        ChartTitle(
            "Skin temperature",
            if (sessionTemp.isEmpty()) "not measured" else readout(sessionTemp, " °C", 1, null),
        )
        ChartCard {
            if (sessionTemp.isEmpty()) {
                EmptyNote(
                    "The watch took no skin temperature during this session.",
                    Modifier.padding(vertical = 24.dp),
                )
            } else {
                ValueChart(
                    points = temperature,
                    window = window,
                    axis = MetricStyle.Temperature.axis,
                    decimals = 1,
                    height = 96.dp,
                    onWindowChange = onWindowChange,
                    scrubAtMs = scrubAtMs,
                    onScrub = { scrubAtMs = it },
                    limit = extent,
                    cursorAlpha = 0.45f,
                    unit = " °C",
                    readout = ChartReadout.Titled,
                )
            }
        }

        if (route != null && route.climb.any { it.value > 0 }) {
            ChartTitle(
                "Elevation gain",
                readout(
                    route.climb,
                    " m",
                    0,
                    "${grouped(route.climb.sumOf { it.value }, 0)} m climbed",
                ),
            )
            ChartCard {
                ValueChart(
                    points = route.climb,
                    window = window,
                    axis = 0.0..10.0,
                    decimals = 0,
                    height = 96.dp,
                    onWindowChange = onWindowChange,
                    scrubAtMs = scrubAtMs,
                    onScrub = { scrubAtMs = it },
                    limit = extent,
                    // A bar for each window the watch reported a climb over,
                    // which is how steeply the ground rose while it did.
                    form = ChartForm.Bars(MINUTE_MS),
                    cursorAlpha = 0.45f,
                    unit = " m",
                    readout = ChartReadout.Titled,
                )
            }
        }

        if (route != null) {
            ChartTitle("Route")
            ChartCard {
                TrackMap(route, tiles, scrubAtMs) { mapFilling = true }
                SpeedLegend(route.summary)
            }
        } else if (entry is RecordedEntry && entry.travel == Travel.GROUND) {
            // Only for a session that was deliberately started: a walk the
            // watch worked out afterwards was never going to have a route, and
            // saying so under every one of them is noise.
            ChartTitle("Route", "no GPS data")
        }
        Spacer(Modifier.height(8.dp))
        }
    }

    if (entry !is RecordedEntry) return

    val session = entry.endedAtMs?.let { Span(entry.startedAtMs, it) }
    if (trimming && session != null) {
        TrimEndDialog(
            span = session,
            scrubbedAtMs = scrubAtMs?.takeIf { it > session.fromMs && it < session.toMs },
            onDismiss = { trimming = false },
            onTrim = { trimming = false; onTrim(entry, it) },
        )
    }

    if (!asking) return
    AlertDialog(
        onDismissRequest = { asking = false },
        containerColor = MaterialTheme.colorScheme.surfaceContainer,
        title = { Text("Delete this session?") },
        text = { Text("Set timings and workout start/end cannot be recovered") },
        confirmButton = {
            TextButton(onClick = { asking = false; onDelete(entry) }) { Text("Delete") }
        },
        dismissButton = {
            TextButton(onClick = { asking = false }) { Text("Cancel") }
        },
    )
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun TrimEndDialog(
    span: Span,
    scrubbedAtMs: Long?,
    onDismiss: () -> Unit,
    onTrim: (Long) -> Unit,
) {
    val seedAtMs = scrubbedAtMs ?: span.toMs
    val seed = remember(seedAtMs) { Calendar.getInstance().apply { timeInMillis = seedAtMs } }
    val picker = rememberTimePickerState(
        initialHour = seed.get(Calendar.HOUR_OF_DAY),
        initialMinute = seed.get(Calendar.MINUTE),
        is24Hour = true,
    )
    // The picker cannot express a second, which is most of a short session. A
    // point picked on the chart can, so it stands until the picker is moved off
    // the minute it sits in.
    val scrubbedMinute = scrubbedAtMs?.takeIf {
        picker.hour == seed.get(Calendar.HOUR_OF_DAY) &&
            picker.minute == seed.get(Calendar.MINUTE)
    }
    val chosen = scrubbedMinute ?: trimEndAtMs(span, picker.hour, picker.minute)

    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = MaterialTheme.colorScheme.surfaceContainer,
        title = { Text("Trim the end") },
        text = {
            Column {
                TimeInput(picker)
                Text(
                    chosen?.let {
                        val at = if (it == scrubbedMinute) clockWithSeconds(it) else clock(it)
                        "Ends $at, ${compactDuration(it - span.fromMs)} of session, " +
                            "${compactDuration(span.toMs - it)} dropped."
                    } ?: "Pick a time between ${clock(span.fromMs)} and ${clock(span.toMs)}.",
                    style = AppTheme.type.rowMeta,
                    color = AppTheme.colors.onSurfaceTertiary,
                )
                Text(
                    "Sets timed after the new end go with it.",
                    Modifier.padding(top = 4.dp),
                    style = AppTheme.type.rowMeta,
                    color = AppTheme.colors.onSurfaceTertiary,
                )
            }
        },
        confirmButton = {
            TextButton(enabled = chosen != null, onClick = { chosen?.let(onTrim) }) { Text("Trim") }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) { Text("Cancel") }
        },
    )
}

private data class Figure(val eyebrow: String, val value: String, val unit: String)

private fun summaryFigures(hr: List<ChartPoint>, temperature: List<ChartPoint>): List<Figure> {
    val rise = temperatureRise(temperature)
    return listOf(
        Figure("avg hr", mean(hr)?.toInt()?.toString() ?: "—", "bpm"),
        Figure("peak hr", hr.maxOfOrNull { it.value }?.toInt()?.toString() ?: "—", "bpm"),
        Figure(
            "temp rise",
            rise?.let { (if (it >= 0) "+" else "−") + grouped(abs(it), 1) } ?: "—",
            "°C",
        ),
    )
}

/**
 * What the route says, which for anything on wheels is everything the
 * pedometer could not: it counts a swinging wrist, not a turning crank.
 */
@Composable
private fun SpeedLegend(summary: TrackSummary) {
    val slow = summary.slowMS
    val fast = summary.fastMS
    if (slow == null || fast == null || fast <= slow) return

    Spacer(Modifier.height(6.dp))
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        Text(
            "${grouped(slow * MS_TO_KMH, 1)} km/h",
            style = AppTheme.type.axisSmall,
            color = AppTheme.colors.onSurfaceDim,
        )
        Box(
            Modifier
                .weight(1f)
                .padding(horizontal = 6.dp)
                .height(4.dp)
                .clip(RoundedCornerShape(2.dp))
                .background(
                    Brush.horizontalGradient(
                        listOf(SLOW_COLOUR, MIDDLING_COLOUR, FAST_COLOUR)
                    )
                )
        )
        Text(
            "${grouped(fast * MS_TO_KMH, 1)} km/h",
            style = AppTheme.type.axisSmall,
            color = AppTheme.colors.onSurfaceDim,
        )
    }
}

/**
 * Two averages, because they answer different questions: over the whole
 * session, which counts every red light, and over the time actually under way,
 * which is how fast it was ridden or run.
 */
private fun paceFigures(
    entry: ActivityEntry,
    route: Route,
    elapsedMs: Long,
): List<Figure> = buildList {
    val summary = route.summary
    val overall = if (elapsedMs > 0) {
        summary.distanceMetres / (elapsedMs / 1000.0)
    } else {
        null
    }
    val moving = summary.averageSpeedMS

    if (entry.onFoot) {
        add(Figure("avg pace", overall?.let(::pacePerKm) ?: "—", "/km"))
        add(Figure("moving pace", moving?.let(::pacePerKm) ?: "—", "/km"))
    } else {
        add(Figure("avg speed", overall?.let { grouped(it * MS_TO_KMH, 1) } ?: "—", "km/h"))
        add(Figure("moving speed", moving?.let { grouped(it * MS_TO_KMH, 1) } ?: "—", "km/h"))
    }
    add(Figure("moving", compactDuration(summary.movingSecs * 1000), ""))
}

private fun effortFigures(
    entry: ActivityEntry,
    totals: ActivityTotals?,
    route: Route?,
): List<Figure> = buildList {
    // Measured ground beats the pedometer's guess at it, and on a bike the
    // pedometer is counting a swinging wrist.
    if (route != null) {
        add(Figure("distance", grouped(route.summary.distanceMetres / 1000, 2), "km"))
    } else if (totals != null && entry.onFoot && totals.steps > 0) {
        add(Figure("steps", grouped(totals.steps), ""))
    }
    // Climbed comes off the barometer, which does not care whether feet did
    // the carrying: a ride gains height the same way a walk does.
    if (totals != null && (entry.onFoot || entry.travel == Travel.GROUND)) {
        add(Figure("climbed", grouped(totals.ascentMetres, 0), "m"))
    }
    entry.calories?.let { add(Figure("energy", grouped(it, 0), "kcal")) }
}

@Composable
private fun FigureRail(figures: List<Figure>) {
    Row(
        Modifier.fillMaxWidth().height(64.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        figures.forEachIndexed { index, figure ->
            if (index > 0) RailRule()
            SummaryFigure(figure.eyebrow, figure.value, figure.unit, Modifier.weight(1f))
        }
    }
}

@Composable
private fun SummaryFigure(eyebrow: String, value: String, unit: String, modifier: Modifier) {
    Column(modifier, horizontalAlignment = Alignment.CenterHorizontally) {
        Eyebrow(eyebrow)
        Spacer(Modifier.height(3.dp))
        ValueWithUnit(value, unit, AppTheme.type.summaryValue)
    }
}

@Composable
private fun RailRule() {
    Box(
        Modifier
            .width(1.dp)
            .fillMaxHeight()
            .padding(vertical = 4.dp)
            .background(MaterialTheme.colorScheme.surfaceVariant)
    )
}

@Composable
fun ChartTitle(title: String, detail: String? = null) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.Bottom) {
        Text(title, Modifier.weight(1f), style = AppTheme.type.sectionTitle)
        if (detail != null) {
            Text(
                detail,
                style = AppTheme.type.rowMeta,
                color = AppTheme.colors.onSurfaceTertiary,
            )
        }
    }
}
