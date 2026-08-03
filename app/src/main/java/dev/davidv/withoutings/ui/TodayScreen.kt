package dev.davidv.withoutings.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Bedtime
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.drawText
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import dev.davidv.withoutings.ui.theme.AppTheme
import uniffi.wpp_ffi.Night
import uniffi.wpp_ffi.SleepStage

/**
 * The day as a shape: what happened, in order, from midnight to now.
 *
 * The ribbon runs top to bottom chronologically, so the present sits at the
 * bottom of the rail — which is also where the thumb is, and why the "now" card
 * is pinned there rather than leading.
 */
@Composable
fun TodayScreen(
    state: UiState,
    nowMs: Long,
    onOpenActivity: (ActivityEntry) -> Unit,
    onOpenSleep: () -> Unit,
) {
    val home = state.home
    val midnight = dayStart(nowMs)
    val sessions = home.today.sortedBy { it.startedAtMs }
    val sleep = home.lastNight?.let { night ->
        night.asleepFromMs?.let { from ->
            night.asleepToMs?.let { to -> Span(from, to) }
        }
    }

    HomeScaffold(
        title = "Today",
        subtitle = "${fullDate(nowMs)} · midnight to now",
        trailing = { BatteryPill(state.snapshot?.battery?.let { "${it.percent}%" } ?: "no reading") },
    ) {
        Row(Modifier.fillMaxSize()) {
            DayRail(
                midnightMs = midnight,
                nowMs = nowMs,
                sleep = sleep,
                sessions = sessions.map { Span(it.startedAtMs, it.endedAtMs ?: nowMs) },
                heartRate = home.hr,
                modifier = Modifier.width(AppTheme.space.railWidth).fillMaxHeight(),
            )
            Column(
                Modifier
                    .weight(1f)
                    .fillMaxHeight()
                    .padding(start = 12.dp)
                    .verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(AppTheme.space.blockTight),
            ) {
                if (sleep != null) SleptCard(home.lastNight, sleep, onOpenSleep)
                sessions.forEach { entry ->
                    EventCard(entry, nowMs) { onOpenActivity(entry) }
                }
                if (sleep == null && sessions.isEmpty()) {
                    EmptyNote(
                        "Nothing staged or recorded since midnight. The rail " +
                            "still shows what the watch measured."
                    )
                }
            }
        }
    }
}

/**
 * Midnight to midnight, drawn once.
 *
 * The whole day is always in view whatever the cards do, because the rail is a
 * scale rather than a list: compressing it to the hours that happen to have
 * something in them would make two identical days look different.
 */
@Composable
private fun DayRail(
    midnightMs: Long,
    nowMs: Long,
    sleep: Span?,
    sessions: List<Span>,
    heartRate: List<ChartPoint>,
    modifier: Modifier = Modifier,
) {
    val measurer = rememberTextMeasurer()
    val colors = AppTheme.colors
    val scheme = MaterialTheme.colorScheme
    val labelStyle = TextStyle(
        fontFamily = androidx.compose.ui.text.font.FontFamily.Monospace,
        fontSize = 9.5.sp,
        color = colors.onSurfaceDim,
    )

    Canvas(modifier) {
        val barWidth = 28.dp.toPx()
        val barLeft = size.width - barWidth
        val dayMs = DAY_MS.toFloat()

        fun y(atMs: Long) = ((atMs - midnightMs) / dayMs) * size.height

        val radius = CornerRadius(barWidth / 2, barWidth / 2)
        drawRoundRect(
            scheme.surfaceContainerHigh,
            topLeft = Offset(barLeft, 0f),
            size = Size(barWidth, size.height),
            cornerRadius = radius,
        )

        if (sleep != null) {
            val top = y(sleep.fromMs).coerceAtLeast(0f)
            val bottom = y(sleep.toMs).coerceAtMost(size.height)
            if (bottom > top) {
                drawRoundRect(
                    colors.sleepRem,
                    topLeft = Offset(barLeft, top),
                    size = Size(barWidth, bottom - top),
                    cornerRadius = radius,
                )
            }
        }

        for (session in sessions) {
            val top = y(session.fromMs).coerceAtLeast(0f)
            val bottom = y(session.toMs).coerceAtMost(size.height)
            // A ten-minute walk is a hair's width over a whole day, and a
            // block that cannot be seen reads as a day with nothing in it.
            val height = (bottom - top).coerceAtLeast(3.dp.toPx())
            drawRoundRect(
                scheme.primary,
                topLeft = Offset(barLeft, top),
                size = Size(barWidth, height),
                cornerRadius = radius,
            )
        }

        // The day's heart rate, laid along the same axis: the trace runs down
        // the bar and its horizontal position is the rate.
        val trace = heartRate.filter { it.atMs in midnightMs..nowMs }.sortedBy { it.atMs }
        if (trace.size > 1) {
            val path = Path()
            trace.forEachIndexed { index, point ->
                val fraction = ((point.value - RAIL_BPM_LOW) / (RAIL_BPM_HIGH - RAIL_BPM_LOW))
                    .coerceIn(0.0, 1.0)
                val x = barLeft + (fraction * barWidth).toFloat()
                val at = y(point.atMs)
                if (index == 0) path.moveTo(x, at) else path.lineTo(x, at)
            }
            drawPath(
                path,
                colors.dataStroke.copy(alpha = 0.8f),
                style = Stroke(
                    width = 1.6.dp.toPx(),
                    cap = StrokeCap.Round,
                    join = StrokeJoin.Round,
                ),
            )
        }

        for (hour in listOf(0, 6, 12, 18)) {
            val label = measurer.measure("%02d".format(hour), labelStyle)
            val at = (hour / 24f) * size.height
            drawText(
                label,
                topLeft = Offset(
                    barLeft - label.size.width - 5.dp.toPx(),
                    at.coerceAtMost(size.height - label.size.height),
                ),
            )
        }
    }
}

/// The rail maps the width of the bar onto the rates a day is normally spent
/// in. Wider would flatten the trace; narrower would clip most of a workout.
private const val RAIL_BPM_LOW = 40.0
private const val RAIL_BPM_HIGH = 120.0

/** The night, and the one accent card on this screen. */
@Composable
private fun SleptCard(night: Night?, sleep: Span, onOpen: () -> Unit) {
    AccentCard(
        Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(AppTheme.radius.card),
        onClick = onOpen,
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Icon(
                Icons.Rounded.Bedtime,
                null,
                Modifier.size(16.dp),
                tint = MaterialTheme.colorScheme.onPrimaryContainer,
            )
            Text(
                "Slept",
                Modifier.padding(start = 5.dp).weight(1f),
                style = AppTheme.type.tileLabel,
                color = MaterialTheme.colorScheme.onPrimaryContainer,
            )
            Text(
                "${clock(sleep.fromMs)} – ${clock(sleep.toMs)}",
                style = AppTheme.type.rowMeta,
                color = AppTheme.colors.onAccentSecondary,
            )
        }
        Spacer(Modifier.height(4.dp))
        val asleep = night?.stages
            ?.filter { it.stage != SleepStage.AWAKE }
            ?.sumOf { it.toMs - it.fromMs }
            ?: 0L
        ValueWithUnit(
            hoursMinutes(asleep),
            night?.score?.let { "asleep · score ${it.total}" } ?: "asleep",
            AppTheme.type.summaryValue,
            color = MaterialTheme.colorScheme.onPrimaryContainer,
            unitColor = AppTheme.colors.onAccentSecondary,
        )
    }
}

/** One thing that happened, with the numbers the watch recorded for it. */
@Composable
private fun EventCard(entry: ActivityEntry, nowMs: Long, onOpen: () -> Unit) {
    Column(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(AppTheme.radius.card))
            .background(MaterialTheme.colorScheme.surfaceContainer)
            .clickable { onOpen() }
            .padding(horizontal = 14.dp, vertical = 11.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(entry.name, Modifier.weight(1f), style = AppTheme.type.rowTitle)
            Text(
                clock(entry.startedAtMs) + (
                    entry.endedAtMs?.let { " · ${compactDuration(it - entry.startedAtMs)}" } ?: ""
                    ),
                style = AppTheme.type.rowMeta,
                color = AppTheme.colors.onSurfaceTertiary,
            )
        }
        val detail = (entry as? DetectedEntry)?.let {
            listOf(
                "${grouped(it.detected.steps)} steps",
                distance(it.detected.distanceMetres),
                "${grouped(it.detected.calories, 0)} kcal",
            ).joinToString(" · ")
        } ?: (
            entry.endedAtMs?.let { "recorded on the watch" }
                ?: "in progress · ${stopwatch(nowMs - entry.startedAtMs)}"
            )
        Text(
            detail,
            Modifier.padding(top = 3.dp),
            style = AppTheme.type.rowMeta,
            color = AppTheme.colors.onSurfaceTertiary,
        )
    }
}
