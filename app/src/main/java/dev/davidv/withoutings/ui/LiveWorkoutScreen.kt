package dev.davidv.withoutings.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.ArrowUpward
import androidx.compose.material.icons.rounded.ExpandMore
import androidx.compose.material.icons.automirrored.rounded.TrendingUp
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.em
import androidx.compose.ui.unit.sp
import dev.davidv.withoutings.ui.theme.AppTheme
import kotlin.math.abs
import uniffi.wpp_ffi.WorkoutSummary

@Composable
fun LiveWorkoutScreen(
    state: UiState,
    workout: WorkoutSummary?,
    window: LongRange,
    nowMs: Long,
    restElapsedMs: Long,
    resting: Boolean,
    following: Boolean,
    onWindowChange: (LongRange) -> Unit,
    onFollowLive: () -> Unit,
    onToggleRest: () -> Unit,
    onStopWorkout: () -> Unit,
    onBack: () -> Unit,
) {
    val hr = state.hr.map { ChartPoint(it.atMs, it.bpm.toDouble()) }
    val started = workout?.startedAtMs
    val elapsed = started?.let { (nowMs - it).coerceAtLeast(0) } ?: 0L
    val sets = state.markers.workSpans(nowMs)
    val space = AppTheme.space
    val maxRate = state.snapshot?.user?.let { maxHeartRate(it.birthSecs, nowMs) }

    Column(
        Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.surface)
            .statusBarsPadding()
            .navigationBarsPadding(),
    ) {
        Row(
            Modifier.fillMaxWidth().padding(horizontal = space.screen - 8.dp, vertical = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            GlyphButton(Icons.Rounded.ExpandMore, "Back", onClick = onBack)
            Column(Modifier.weight(1f).padding(start = 2.dp)) {
                Text(workout?.activity ?: "Session ended", style = AppTheme.type.cardTitle)
                Text(
                    started?.let { "started ${clock(it)} · on watch" }
                        ?: "the watch is no longer recording",
                    style = AppTheme.type.rowMeta,
                    color = AppTheme.colors.onSurfaceTertiary,
                )
            }
            if (started != null) {
                Text(
                    stopwatch(elapsed),
                    style = AppTheme.type.focalMedium.copy(fontSize = 30.sp),
                    color = MaterialTheme.colorScheme.onSurface,
                )
            }
        }

        Column(
            Modifier.weight(1f).padding(horizontal = space.screen).padding(top = 8.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            FocalHeartRate(hr)
            ZoneBar(hr.maxByOrNull { it.atMs }?.value, maxRate)
            Observation(hr, state.workoutTemp)

            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.Bottom) {
                Eyebrow("hr · this session", Modifier.weight(1f))
                Eyebrow(
                    hr.firstOrNull()?.let { first ->
                        "${first.value.toInt()} → ${hr.maxOf { it.value }.toInt()}"
                    } ?: "no samples yet",
                )
            }
            ChartCard(Modifier.weight(1f)) {
                ValueChart(
                    points = hr,
                    window = window,
                    axis = MetricStyle.HeartRate.axis,
                    decimals = 0,
                    modifier = Modifier.fillMaxHeight(),
                    height = null,
                    onWindowChange = onWindowChange,
                    sessions = sets.chartSessions("set"),
                    limit = started?.let { it..nowMs },
                    unit = " bpm",
                )
            }
            Text(
                "shading marks the sets you timed here",
                style = AppTheme.type.axisSmall,
                color = AppTheme.colors.onSurfaceDim,
            )

            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                SkinTempTile(state.workoutTemp, Modifier.weight(1f))
                RestTile(sets.size, resting, restElapsedMs, Modifier.weight(1f))
            }
        }

        Column(
            Modifier.padding(horizontal = space.screen).padding(top = 14.dp, bottom = 26.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                if (!following) {
                    FilledAction(
                        "Jump to now",
                        Modifier.weight(1f),
                        shape = RoundedCornerShape(AppTheme.radius.button),
                        container = MaterialTheme.colorScheme.surfaceContainerHigh,
                        content = MaterialTheme.colorScheme.onSurface,
                        onClick = onFollowLive,
                    )
                }
                FilledAction(
                    if (resting) "End rest  ${stopwatch(restElapsedMs)}" else "Start rest",
                    Modifier.weight(1f),
                    shape = RoundedCornerShape(AppTheme.radius.button),
                    onClick = onToggleRest,
                )
            }
            if (started != null) {
                OutlineAction("Finish session", Modifier.fillMaxWidth(), onClick = onStopWorkout)
            }
            Text(
                "The rest timer is this app's own. Finishing asks the watch, which " +
                    "is where the session actually ends; pausing is on the watch.",
                style = AppTheme.type.axisSmall,
                color = AppTheme.colors.onSurfaceDim,
            )
        }
    }
}

@Composable
private fun FocalHeartRate(hr: List<ChartPoint>) {
    val latest = hr.maxByOrNull { it.atMs }?.value
    Row(verticalAlignment = Alignment.Bottom) {
        Text(
            latest?.toInt()?.toString() ?: "—",
            style = AppTheme.type.focalLarge,
            color = MaterialTheme.colorScheme.primary,
        )
        Column(Modifier.padding(start = 10.dp, bottom = 8.dp)) {
            Text(
                "BPM",
                style = AppTheme.type.eyebrowLarge.copy(letterSpacing = 0.14.em),
                color = MaterialTheme.colorScheme.primary,
            )
            Text(
                if (hr.isEmpty()) {
                    "no samples yet"
                } else {
                    "max ${hr.maxOf { it.value }.toInt()} · " +
                        "avg ${mean(hr)?.toInt() ?: 0}"
                },
                style = AppTheme.type.rowMeta,
                color = AppTheme.colors.onSurfaceTertiary,
            )
        }
    }
}

@Composable
private fun ZoneBar(bpm: Double?, maxRate: Int?) {
    val current = bpm?.let { zoneOf(it, maxRate) }
    val floors = maxRate?.let { zoneFloors(it) }
    Column {
        Row(
            Modifier.fillMaxWidth().height(12.dp),
            horizontalArrangement = Arrangement.spacedBy(3.dp),
        ) {
            HeartRateZone.entries.forEach { zone ->
                Box(
                    Modifier
                        .weight(1f)
                        .fillMaxHeight()
                        .clip(RoundedCornerShape(AppTheme.radius.segment))
                        .background(
                            if (zone == current) {
                                MaterialTheme.colorScheme.primary
                            } else {
                                AppTheme.colors.track
                            }
                        )
                )
            }
        }
        Text(
            when {
                floors == null ->
                    "Zones need the watch's profile. Sync, and set it under Watch › Wearer."
                current == null -> "zones ${floors.drop(1).joinToString(" · ")} bpm · max $maxRate"
                else -> "${current.label} · zones ${floors.drop(1).joinToString(" · ")} bpm"
            },
            Modifier.padding(top = 5.dp),
            style = AppTheme.type.axisSmall,
            color = AppTheme.colors.onSurfaceTertiary,
        )
    }
}

@Composable
private fun Observation(hr: List<ChartPoint>, temperature: List<ChartPoint>) {
    if (hr.isEmpty()) {
        AccentCard(Modifier.fillMaxWidth(), shape = RoundedCornerShape(AppTheme.radius.card)) {
            Text(
                "Nothing measured yet",
                style = AppTheme.type.sectionTitle,
                color = MaterialTheme.colorScheme.onPrimaryContainer,
            )
            Text(
                "The watch sends the session's samples as it takes them.",
                style = AppTheme.type.body,
                color = AppTheme.colors.onAccentSecondary,
            )
        }
        return
    }

    val peak = hr.maxOf { it.value }
    val nearPeak = hr.filter { it.value >= peak - CEILING_BAND_BPM }
    val ceilingMs = nearPeak.size * samplingIntervalMs(hr)
    val rise = temperature.takeIf { it.size > 1 }?.let {
        val ordered = it.sortedBy { point -> point.atMs }
        ordered.last().value - ordered.first().value
    }

    AccentCard(Modifier.fillMaxWidth(), shape = RoundedCornerShape(AppTheme.radius.card)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Icon(
                Icons.AutoMirrored.Rounded.TrendingUp,
                null,
                Modifier.size(20.dp),
                tint = MaterialTheme.colorScheme.onPrimaryContainer,
            )
            Text(
                "${compactDuration(ceilingMs)} within " +
                    "${CEILING_BAND_BPM.toInt()} of this session's peak",
                Modifier.padding(start = 8.dp),
                style = AppTheme.type.sectionTitle,
                color = MaterialTheme.colorScheme.onPrimaryContainer,
            )
        }
        Text(
            buildString {
                append("Peak ${peak.toInt()} bpm")
                if (rise != null) {
                    append(", skin temperature ")
                    append(if (rise >= 0) "up " else "down ")
                    append(grouped(abs(rise), 1))
                    append(" °C since it started")
                }
                append(".")
            },
            Modifier.padding(top = 4.dp),
            style = AppTheme.type.body,
            color = AppTheme.colors.onAccentSecondary,
        )
    }
}

private const val CEILING_BAND_BPM = 5.0

@Composable
private fun SkinTempTile(temperature: List<ChartPoint>, modifier: Modifier) {
    val ordered = temperature.sortedBy { it.atMs }
    val latest = ordered.lastOrNull()?.value
    val rise = if (ordered.size > 1) ordered.last().value - ordered.first().value else null
    Tile(modifier) {
        Eyebrow("skin temp")
        Spacer(Modifier.height(4.dp))
        Row(verticalAlignment = Alignment.Bottom) {
            Text(
                latest?.let { grouped(it, 1) } ?: "—",
                style = AppTheme.type.summaryValue,
            )
            if (rise != null && abs(rise) >= 0.1) {
                Icon(
                    Icons.Rounded.ArrowUpward,
                    null,
                    Modifier.padding(start = 4.dp, bottom = 3.dp).size(17.dp),
                    tint = MaterialTheme.colorScheme.primary,
                )
                Text(
                    grouped(abs(rise), 1),
                    Modifier.padding(bottom = 3.dp),
                    style = AppTheme.type.rowMeta,
                    color = AppTheme.colors.onSurfaceTertiary,
                )
            }
        }
        if (latest == null) {
            Text(
                "not measured in this session",
                style = AppTheme.type.tileContext,
                color = AppTheme.colors.onSurfaceTertiary,
            )
        }
    }
}

@Composable
private fun RestTile(sets: Int, resting: Boolean, elapsedMs: Long, modifier: Modifier) {
    Tile(modifier) {
        Eyebrow(if (resting) "set ${sets + 1} · rest" else "sets timed")
        Spacer(Modifier.height(4.dp))
        Text(
            if (resting) stopwatch(elapsedMs) else sets.toString(),
            style = AppTheme.type.summaryValue,
        )
        Spacer(Modifier.height(6.dp))
        if (resting) {
            TrackBar(1f, Modifier.fillMaxWidth(), height = 5.dp)
        } else {
            Text(
                "start a rest to mark one",
                style = AppTheme.type.tileContext,
                color = AppTheme.colors.onSurfaceTertiary,
            )
        }
    }
}
