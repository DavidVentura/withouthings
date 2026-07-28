package dev.davidv.withoutings.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import dev.davidv.withoutings.LinkState
import uniffi.wpp_ffi.Progress
import uniffi.wpp_ffi.Snapshot
import uniffi.wpp_ffi.WorkoutSummary

private val clock = SimpleDateFormat("HH:mm:ss", Locale.getDefault())
private val hourMinute = SimpleDateFormat("HH:mm", Locale.getDefault())
private val stamp = SimpleDateFormat("d MMM HH:mm", Locale.getDefault())

/**
 * A reading older than this says nothing about now, so it is shown as absent
 * rather than as a current value.
 */
private const val STALE_AFTER_MS = 30 * 60 * 1000L

/** Local midnight, for deciding whether a daily total is actually today's. */
private fun todayStartMs(): Long = java.util.Calendar.getInstance().apply {
    set(java.util.Calendar.HOUR_OF_DAY, 0)
    set(java.util.Calendar.MINUTE, 0)
    set(java.util.Calendar.SECOND, 0)
    set(java.util.Calendar.MILLISECOND, 0)
}.timeInMillis

private fun age(atMs: Long): String {
    val delta = System.currentTimeMillis() - atMs
    return when {
        delta < 60_000 -> "just now"
        delta < 3_600_000 -> "${delta / 60_000} min ago"
        atMs >= todayStartMs() -> clock.format(Date(atMs))
        else -> stamp.format(Date(atMs))
    }
}

@Composable
fun SetupScreen(onSave: (String, String) -> Unit) {
    var mac by remember { mutableStateOf("") }
    var secret by remember { mutableStateOf("") }
    Column(Modifier.fillMaxSize().statusBarsPadding().padding(24.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text("Pair a watch", style = MaterialTheme.typography.headlineSmall)
        Text(
            "The secret is the 32-character association key the watch was set up with.",
            style = MaterialTheme.typography.bodySmall,
        )
        OutlinedTextField(
            mac, { mac = it },
            label = { Text("MAC address") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            secret, { secret = it },
            label = { Text("Account secret") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Button(
            onClick = { onSave(mac.trim(), secret.trim()) },
            enabled = mac.isNotBlank() && secret.isNotBlank(),
        ) { Text("Connect") }
    }
}

@Composable
fun IdleScreen(
    state: UiState,
    onOpenWorkouts: () -> Unit,
    onOpenEcgs: () -> Unit,
    onOpenScreens: () -> Unit,
    onOpenMetric: (MetricStyle) -> Unit,
    onOpenDevice: () -> Unit,
    onRefresh: () -> Unit,
) {
    val snapshot = state.snapshot
    Column(Modifier.fillMaxSize().statusBarsPadding().padding(20.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        Text("Withoutings", style = MaterialTheme.typography.headlineMedium)
        Text(statusLine(state.link, snapshot), style = MaterialTheme.typography.bodySmall)
        // Only while a charger is actually delivering, and only from a reading
        // recent enough to describe now: a stale "charging" would assert the
        // very thing you came here to check.
        if (snapshot?.battery?.charging == true) {
            Text(
                "charging",
                style = MaterialTheme.typography.labelLarge,
                color = MaterialTheme.colorScheme.primary,
            )
        }
        snapshot?.let { SyncProgressRow(it) }

        val now = System.currentTimeMillis()
        // A daily total belongs to its day; a reading belongs to its instant.
        // Neither says anything about now once it is old enough.
        val steps = snapshot?.steps?.takeIf { it.dayStartMs >= todayStartMs() }

        MetricStyle.entries.chunked(2).forEach { row ->
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                row.forEach { style ->
                    val reading = state.latest[style]
                    val fresh = when (style) {
                        MetricStyle.Steps -> steps != null
                        else -> reading != null && now - reading.atMs < STALE_AFTER_MS
                    }
                    val shown = when (style) {
                        MetricStyle.Steps -> steps?.count?.toString()
                        else -> reading?.let { format(it.value, style) }
                    }
                    Stat(
                        style.label,
                        if (fresh) shown ?: "—" else "—",
                        when {
                            fresh -> "${style.unit} · ${age(reading?.atMs ?: now)}"
                            reading != null -> "last ${age(reading.atMs)}"
                            else -> style.unit
                        },
                        Modifier.weight(1f),
                    ) { onOpenMetric(style) }
                }
                if (row.size == 1) Spacer(Modifier.weight(1f))
            }
        }

        Button(onClick = onRefresh) { Text("Refresh from watch") }
        Button(onClick = onOpenWorkouts) { Text("Workouts (${state.workouts.size})") }
        Button(onClick = onOpenEcgs) { Text("ECG (${state.ecgs.size})") }
        Button(onClick = onOpenScreens) { Text("Watch screens") }
        Button(onClick = onOpenDevice) { Text("Watch settings") }

        if (snapshot != null && snapshot.pendingDeletes > 0u) {
            Text(
                "${snapshot.pendingDeletes} measurement(s) read but not yet committed",
                style = MaterialTheme.typography.labelSmall,
            )
        }
    }
}

@Composable
fun WorkoutScreen(
    state: UiState,
    /// The workout being looked at: the running one, or one picked from the
    /// list. Reading the live one only left a finished workout unlabelled.
    workout: WorkoutSummary?,
    window: LongRange,
    elapsedMs: Long,
    running: Boolean,
    /// False once panning or zooming has detached the view from the live edge.
    following: Boolean,
    onWindowChange: (LongRange) -> Unit,
    onFollowLive: () -> Unit,
    onToggleStopwatch: () -> Unit,
) {
    // A finished workout is a closed interval; a running one keeps growing.
    val workoutLimit = workout?.let {
        it.startedAtMs..(it.endedAtMs ?: System.currentTimeMillis())
    }
    // Only a running workout has a "now" to jump to or a rest to time.
    val live = workout != null && workout.endedAtMs == null
    Column(Modifier.fillMaxSize().statusBarsPadding().padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(
            workout?.activity ?: "Workout",
            style = MaterialTheme.typography.headlineMedium,
        )
        Text(
            workout?.let {
                val until = it.endedAtMs ?: System.currentTimeMillis()
                "Start: ${hourMinute.format(Date(it.startedAtMs))} · " +
                    "Duration: ${formatElapsed(until - it.startedAtMs)}"
            } ?: "no workout selected",
            style = MaterialTheme.typography.bodySmall,
        )

        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Stat("Latest HR", state.hr.lastOrNull()?.bpm?.toString() ?: "—", "bpm", Modifier.weight(1f))
            Stat("Peak HR", state.hr.maxOfOrNull { it.bpm }?.toString() ?: "—", "bpm", Modifier.weight(1f))
            Stat(
                "Latest temp",
                state.workoutTemp.lastOrNull()?.let { String.format(Locale.US, "%.2f", it.value) }
                    ?: "—",
                "°C",
                Modifier.weight(1f),
            )
        }

        Text("Heart rate", style = MaterialTheme.typography.labelSmall)
        ValueChart(
            points = state.hr.map { ChartPoint(it.atMs, it.bpm.toDouble()) },
            markers = state.markers,
            window = window,
            onWindowChange = onWindowChange,
            axis = 30.0..200.0,
            decimals = 0,
            limit = workoutLimit,
            height = 190.dp,
            lineColor = MaterialTheme.colorScheme.primary,
            gridColor = MaterialTheme.colorScheme.outlineVariant,
            axisColor = MaterialTheme.colorScheme.outline,
            labelColor = MaterialTheme.colorScheme.onSurfaceVariant,
            setColor = MaterialTheme.colorScheme.primary.copy(alpha = 0.12f),
        )

        // Stacked under the trace on the same window, so the two read as one
        // picture: panning or zooming either moves both.
        Text("Temperature", style = MaterialTheme.typography.labelSmall)
        ValueChart(
            points = state.workoutTemp,
            markers = state.markers,
            window = window,
            onWindowChange = onWindowChange,
            axis = 36.0..38.5,
            decimals = 2,
            limit = workoutLimit,
            height = 150.dp,
            lineColor = MaterialTheme.colorScheme.tertiary,
            gridColor = MaterialTheme.colorScheme.outlineVariant,
            axisColor = MaterialTheme.colorScheme.outline,
            labelColor = MaterialTheme.colorScheme.onSurfaceVariant,
            setColor = MaterialTheme.colorScheme.tertiary.copy(alpha = 0.12f),
        )

        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            // Timing the rest only means anything while a workout is running.
            if (live) {
                Button(onClick = onToggleStopwatch, modifier = Modifier.weight(1f)) {
                    Text(if (running) "End rest  ${formatElapsed(elapsedMs)}" else "Start rest")
                }
            }
            // The chart follows the live edge until panned away; this is the
            // way back, and there is no live edge on a finished workout.
            if (live && !following) {
                Button(onClick = onFollowLive, modifier = Modifier.weight(1f)) {
                    Text("Jump to now")
                }
            }
        }
    }
}

@Composable
fun WorkoutsScreen(workouts: List<WorkoutSummary>, onSelect: (WorkoutSummary) -> Unit) {
    Column(Modifier.fillMaxSize().statusBarsPadding().padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("Workouts", style = MaterialTheme.typography.headlineMedium)
        LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            items(workouts) { workout ->
                Card(Modifier.fillMaxWidth().clickable { onSelect(workout) }) {
                    Column(Modifier.padding(12.dp)) {
                        Text(workout.activity, style = MaterialTheme.typography.titleMedium)
                        Text(
                            stamp.format(Date(workout.startedAtMs)),
                            style = MaterialTheme.typography.bodyMedium,
                        )
                        Text(
                            workout.endedAtMs?.let {
                                "duration ${formatElapsed(it - workout.startedAtMs)}"
                            } ?: "in progress",
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                }
            }
        }
    }
}

/**
 * A transfer reports exact bytes; the history walk can only estimate, so it is
 * labelled rather than dressed up as a percentage.
 */
@Composable
private fun SyncProgressRow(snapshot: Snapshot) {
    val sync = snapshot.sync
    val received = sync.transferReceived
    val total = sync.transferTotal
    when {
        received != null && total != null && total > 0u -> {
            val fraction = received.toFloat() / total.toFloat()
            Column {
                Text(
                    "measurement transfer  ${received / 1024u} of ${total / 1024u} KiB",
                    style = MaterialTheme.typography.labelSmall,
                )
                LinearProgressIndicator(
                    progress = { fraction },
                    modifier = Modifier.fillMaxWidth(),
                )
            }
        }

        snapshot.progress == Progress.SYNCING -> Column {
            val fraction = sync.historyFraction
            // The fraction covers the stream in progress only, so it is shown
            // next to the stream count rather than as overall progress.
            val stream = "stream ${sync.streamsDone + 1u} of ${sync.streamsTotal}"
            Text(
                if (fraction != null) "catching up  $stream  ${(fraction * 100).toInt()}%"
                else "catching up  $stream",
                style = MaterialTheme.typography.labelSmall,
            )
            if (fraction != null) {
                LinearProgressIndicator(
                    progress = { fraction.toFloat() },
                    modifier = Modifier.fillMaxWidth(),
                )
            } else {
                LinearProgressIndicator(Modifier.fillMaxWidth())
            }
        }

        else -> {}
    }
    if (sync.recordsStored > 0uL) {
        Text(
            "${sync.recordsStored} records stored this session",
            style = MaterialTheme.typography.labelSmall,
        )
    }
}

@Composable
fun Stat(
    label: String,
    value: String,
    unit: String,
    modifier: Modifier = Modifier,
    onClick: (() -> Unit)? = null,
) {
    Card(if (onClick != null) modifier.clickable { onClick() } else modifier) {
        Column(
            Modifier.padding(12.dp).fillMaxWidth(),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(label, style = MaterialTheme.typography.labelMedium)
            Text(value, style = MaterialTheme.typography.headlineMedium, textAlign = TextAlign.Center)
            if (unit.isNotEmpty()) Text(unit, style = MaterialTheme.typography.labelSmall)
        }
    }
}

/**
 * Link state and sync phase are different things: the watch can be connected
 * while nothing is syncing, and the old wording hid a link that never came up.
 */
private fun format(value: Double, style: MetricStyle): String =
    if (style.decimals == 0) value.toInt().toString()
    else String.format(Locale.US, "%.${style.decimals}f", value)

private fun statusLine(link: LinkState, snapshot: Snapshot?): String = when (link) {
    LinkState.Disconnected -> "not connected"
    LinkState.Connecting -> "connecting to watch"
    LinkState.Connected -> "connected, subscribing"
    LinkState.Ready -> when (snapshot?.progress) {
        null, Progress.IDLE -> "connected"
        Progress.CONNECTING -> "connected, authenticating"
        Progress.SYNCING -> "connected, syncing"
        Progress.FINISHED -> "connected, synced"
        Progress.NOT_AUTHENTICATED -> "connected, but the watch refused the secret"
    }
}

private fun formatElapsed(ms: Long): String {
    val total = ms / 1000
    return String.format(Locale.US, "%d:%02d", total / 60, total % 60)
}
