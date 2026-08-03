package dev.davidv.withoutings.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Bedtime
import androidx.compose.material.icons.automirrored.rounded.DirectionsWalk
import androidx.compose.material.icons.rounded.FitnessCenter
import androidx.compose.material.icons.rounded.PlayArrow
import androidx.compose.material.icons.rounded.Settings
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.unit.dp
import dev.davidv.withoutings.LinkState
import dev.davidv.withoutings.ui.theme.AppTheme
import uniffi.wpp_ffi.Activity
import uniffi.wpp_ffi.Night
import uniffi.wpp_ffi.SleepStage

@Composable
fun NowScreen(
    state: UiState,
    nowMs: Long,
    onOpenMetric: (MetricStyle) -> Unit,
    onOpenSleep: () -> Unit,
    onOpenActivities: () -> Unit,
    onOpenActivity: (ActivityEntry) -> Unit,
    onOpenLive: () -> Unit,
    onOpenSettings: () -> Unit,
    onStartWorkout: (UInt) -> Unit,
) {
    val home = state.home
    HomeScaffold(
        title = "Now",
        subtitle = "${fullDate(nowMs)} · ${clock(nowMs)}",
        trailing = {
            BatteryPill(
                batteryLabel(state),
                dotColor = linkDot(state),
                onClick = { onOpenMetric(MetricStyle.Battery) },
            )
            GlyphButton(Icons.Rounded.Settings, "App settings", onClick = onOpenSettings)
        },
    ) {
        MetricStyle.HOME.chunked(2).forEach { row ->
            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(AppTheme.space.blockTight),
            ) {
                row.forEach { style ->
                    val tile = homeTile(style, state, nowMs)
                    MetricTile(
                        icon = style.icon,
                        label = style.label,
                        value = tile.value,
                        unit = tile.unit,
                        context = tile.context,
                        modifier = Modifier.weight(1f).height(TILE_HEIGHT),
                    ) { onOpenMetric(style) }
                }
            }
        }

        LastNightCard(home.lastNight, onOpenSleep)

        SectionHeader("Recent activity", action = "See all", onAction = onOpenActivities)
        val recent = state.activityLog
            .filter { it.startedAtMs >= nowMs - RECENT_SPAN_MS }
            .sortedByDescending { it.startedAtMs }
            .take(RECENT_ROWS)
        if (recent.isEmpty()) {
            EmptyNote(
                "Nothing in the last week. Workouts started on the watch appear " +
                    "here once it has synced, and walks are picked out of the " +
                    "activity it counts on its own."
            )
        } else {
            recent.forEachIndexed { index, entry ->
                if (index > 0) RowDivider()
                EntityRow(
                    icon = if (entry is RecordedEntry) {
                        Icons.Rounded.FitnessCenter
                    } else {
                        Icons.AutoMirrored.Rounded.DirectionsWalk
                    },
                    title = entry.name,
                    meta = entryMeta(entry, nowMs),
                    accent = entry is RecordedEntry,
                ) { onOpenActivity(entry) }
            }
        }

        Spacer(Modifier.weight(1f))
        SessionBlock(state, nowMs, onOpenLive, onStartWorkout)
    }
}

private val TILE_HEIGHT = 104.dp

private const val RECENT_ROWS = 5
private const val RECENT_SPAN_MS = 7 * DAY_MS

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun SessionBlock(
    state: UiState,
    nowMs: Long,
    onOpenLive: () -> Unit,
    onStartWorkout: (UInt) -> Unit,
) {
    val active = state.snapshot?.activeWorkout
    if (active != null) {
        FilledAction(
            "${active.activity} · ${stopwatch(nowMs - active.startedAtMs)}",
            Modifier.fillMaxWidth(),
            icon = Icons.Rounded.PlayArrow,
            onClick = onOpenLive,
        )
        return
    }

    var picking by remember { mutableStateOf(false) }
    val menu = state.activities.sortedByDescending { it.enabled }
    val ready = state.link == LinkState.Ready && menu.isNotEmpty()

    Column(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(AppTheme.radius.card))
            .background(MaterialTheme.colorScheme.surfaceContainerHigh)
            .clickable(enabled = ready) { picking = true }
            .padding(horizontal = 16.dp, vertical = 14.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Icon(
                Icons.Rounded.PlayArrow,
                null,
                Modifier.size(20.dp),
                tint = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Text(
                "Start a workout",
                style = AppTheme.type.buttonLabel,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }

    if (!picking) return
    ModalBottomSheet(onDismissRequest = { picking = false }) {
        Column(
            Modifier
                .padding(horizontal = AppTheme.space.screen)
                .padding(bottom = 32.dp)
        ) {
            menu.forEachIndexed { index, activity ->
                if (index > 0 && menu[index - 1].enabled && !activity.enabled) {
                    Box(
                        Modifier
                            .fillMaxWidth()
                            .padding(vertical = 8.dp)
                            .height(1.dp)
                            .background(MaterialTheme.colorScheme.onSurfaceVariant)
                    )
                } else if (index > 0) {
                    RowDivider(inset = 0.dp)
                }
                Row(
                    Modifier
                        .fillMaxWidth()
                        .clickable {
                            picking = false
                            onStartWorkout(activity.id)
                        }
                        .padding(vertical = 14.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(activity.name, Modifier.weight(1f), style = AppTheme.type.rowTitle)
                    Icon(
                        Icons.Rounded.PlayArrow,
                        null,
                        Modifier.size(18.dp),
                        tint = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }
    }
}

@Composable
private fun LastNightCard(night: Night?, onOpen: () -> Unit) {
    Column(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(AppTheme.radius.tile))
            .background(MaterialTheme.colorScheme.surfaceContainer)
            .clickable { onOpen() }
            .padding(start = 15.dp, end = 15.dp, top = 12.dp, bottom = 10.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(5.dp),
                ) {
                    Icon(
                        Icons.Rounded.Bedtime,
                        null,
                        Modifier.size(16.dp),
                        tint = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    Text(
                        "Last night",
                        style = AppTheme.type.tileLabel,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                Spacer(Modifier.height(4.dp))
                val asleepMs = night?.asleepMs() ?: 0L
                if (night == null || night.stages.isEmpty()) {
                    EmptyNote("The watch staged no sleep for last night.")
                } else {
                    ValueWithUnit(
                        hoursMinutes(asleepMs),
                        night.score?.let { "asleep · score ${it.total}" } ?: "asleep",
                        AppTheme.type.statValue,
                    )
                }
            }
            RowChevron()
        }
        if (night != null && night.stages.isNotEmpty()) {
            Spacer(Modifier.height(10.dp))
            HypnogramStrip(night.stages, Modifier.fillMaxWidth().height(26.dp))
        }
    }
}

private fun Night.asleepMs(): Long = stages
    .filter { it.stage != SleepStage.AWAKE }
    .sumOf { it.toMs - it.fromMs }

private data class Tile(val value: String, val unit: String, val context: String)

@Composable
private fun homeTile(style: MetricStyle, state: UiState, nowMs: Long): Tile {
    val home = state.home
    val reading = state.latest[style]
    val staleAt = reading?.atMs?.takeIf { nowMs - it > style.freshFor }

    fun aged(line: String) = staleAt?.let { "${freshness(it, nowMs)} · $line" } ?: line

    return when (style) {
        MetricStyle.HeartRate -> {
            val resting = restingRate(home.hr)
            Tile(
                value = reading?.value?.toInt()?.toString() ?: "—",
                unit = if (reading == null) "" else "bpm",
                context = when {
                    reading == null -> "not measured today"
                    resting == null -> aged("no resting rate yet")
                    else -> aged("resting ${resting.toInt()}")
                },
            )
        }

        MetricStyle.Steps -> {
            val steps = state.snapshot?.steps?.takeIf { it.dayStartMs >= dayStart(nowMs) }
            Tile(
                value = steps?.let { grouped(it.count.toLong()) } ?: "—",
                unit = if (steps == null) "" else "steps",
                context = when {
                    steps == null -> "not counted today"
                    home.distanceMetres == null -> "distance not counted yet"
                    else -> "${distance(home.distanceMetres)} walked"
                },
            )
        }

        MetricStyle.Calories -> {
            val active = home.today.filterIsInstance<DetectedEntry>().sumOf { it.detected.calories }
            Tile(
                value = home.calories?.let { grouped(it, 0) } ?: "—",
                unit = if (home.calories == null) "" else "kcal",
                context = when {
                    home.calories == null -> "not counted today"
                    active <= 0 -> "no recorded activity yet"
                    else -> "${grouped(active, 0)} kcal in walks"
                },
            )
        }

        else -> {
            val baseline = percentile(home.fortnightTemperature.map { it.value }, 0.5)
            val value = reading?.value
            Tile(
                value = value?.let { grouped(it, style.decimals) } ?: "—",
                unit = if (value == null) "" else style.unit,
                context = when {
                    value == null -> "not measured today"
                    baseline == null -> aged("no baseline yet")
                    kotlin.math.abs(value - baseline) < 0.15 -> aged("at your baseline")
                    else -> aged(
                        "${grouped(kotlin.math.abs(value - baseline), 1)} " +
                            (if (value > baseline) "above" else "below") + " baseline"
                    )
                },
            )
        }
    }
}

private fun batteryLabel(state: UiState): String {
    val battery = state.snapshot?.battery ?: return "no reading"
    if (battery.charging == true) return "${battery.percent}% ⚡"
    return "${battery.percent}%"
}

@Composable
private fun linkDot(state: UiState) = when (state.link) {
    LinkState.Ready -> MaterialTheme.colorScheme.primary
    LinkState.Disconnected -> AppTheme.colors.onSurfaceDim
    else -> AppTheme.colors.sleepRem
}

fun entryMeta(entry: ActivityEntry, nowMs: Long, withDay: Boolean = true): String = sessionMeta(
    entry.startedAtMs,
    entry.endedAtMs,
    nowMs,
    withDay = withDay,
    extra = (entry as? DetectedEntry)?.let {
        listOf(distance(it.detected.distanceMetres))
    } ?: emptyList(),
)

@Composable
fun HypnogramStrip(stages: List<uniffi.wpp_ffi.SleepBand>, modifier: Modifier = Modifier) {
    val ordered = stages.sortedBy { it.fromMs }
    val total = ordered.sumOf { it.toMs - it.fromMs }.toFloat()
    if (total <= 0) return
    Row(modifier.clip(RoundedCornerShape(4.dp))) {
        ordered.forEach { band ->
            Box(
                Modifier
                    .weight(((band.toMs - band.fromMs) / total).coerceAtLeast(0.001f))
                    .fillMaxHeight()
                    .background(stageColor(band.stage))
            )
        }
    }
}
