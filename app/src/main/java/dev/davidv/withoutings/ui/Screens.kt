package dev.davidv.withoutings.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
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
import kotlinx.coroutines.delay
import dev.davidv.withoutings.LinkState
import uniffi.wpp_ffi.EcgSummary
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

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun IdleScreen(
    state: UiState,
    onOpenWorkouts: () -> Unit,
    onOpenEcgs: () -> Unit,
    onOpenMetric: (MetricStyle) -> Unit,
    onOpenSettings: () -> Unit,
    onRefresh: () -> Unit,
) {
    val snapshot = state.snapshot
    // The spinner belongs to the gesture, not to the link: the app syncs on its
    // own several times a minute, and spinning for that would be reporting
    // someone else's work as the answer to a pull nobody made.
    var pulled by remember { mutableStateOf(false) }
    val syncing = snapshot?.progress == Progress.SYNCING
    LaunchedEffect(pulled, syncing) {
        if (!pulled) return@LaunchedEffect
        // Held while the sync the pull asked for runs; the delay is what covers
        // a refresh the watch declines to make, which reports nothing at all.
        if (syncing) return@LaunchedEffect
        delay(PULL_SETTLE_MS)
        pulled = false
    }

    Page(
        title = "Withoutings",
        actions = {
            IconButton(onClick = onOpenSettings) {
                Icon(Icons.Filled.Settings, "Watch settings")
            }
        },
    ) {
        PullToRefreshBox(
            isRefreshing = pulled,
            onRefresh = {
                pulled = true
                onRefresh()
            },
            modifier = Modifier.weight(1f),
        ) {
            Column(
                Modifier.fillMaxSize().verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                val (linkText, tone) = linkChip(state.link, snapshot?.progress)
                StatusRow {
                    StatusChip(linkText, tone)
                    syncLabel(snapshot)?.let { StatusChip(it, Tone.Working) }
                    // The battery says something about the watch rather than
                    // about you, so it belongs beside the link rather than
                    // among the day's readings. Charging is only ever claimed
                    // from a reading recent enough to describe now: a stale one
                    // would assert the very thing you came here to check.
                    snapshot?.battery?.let { battery ->
                        val charging = battery.charging == true
                        StatusChip(
                            if (charging) "${battery.percent}% · charging"
                            else "${battery.percent}% charge",
                            if (charging) Tone.Good else Tone.Quiet,
                        ) { onOpenMetric(MetricStyle.Battery) }
                    }
                }

                val now = System.currentTimeMillis()
                // A daily total belongs to its day; a reading belongs to its
                // instant. Neither says anything about now once it is old enough.
                val steps = snapshot?.steps?.takeIf { it.dayStartMs >= todayStartMs() }

                // The battery has its own pill above; the grid is what the
                // watch measured about you.
                MetricStyle.entries.filter { it != MetricStyle.Battery }.chunked(2).forEach { row ->
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

                NavRow("Workouts", workoutsDetail(state.workouts), onOpenWorkouts)
                NavRow("ECG", ecgsDetail(state.ecgs), onOpenEcgs)

                if (snapshot != null && snapshot.pendingDeletes > 0u) {
                    Text(
                        "${snapshot.pendingDeletes} measurement(s) read but not yet committed",
                        style = MaterialTheme.typography.labelSmall,
                    )
                }
                Spacer(Modifier.height(16.dp))
            }
        }
    }
}

private fun workoutsDetail(workouts: List<WorkoutSummary>): String {
    val latest = workouts.maxByOrNull { it.startedAtMs } ?: return "none recorded"
    return "${workouts.size} · latest ${latest.activity}, ${age(latest.startedAtMs)}"
}

private fun ecgsDetail(recordings: List<EcgSummary>): String {
    val latest = recordings.maxByOrNull { it.measuredAtMs } ?: return "none recorded"
    return "${recordings.size} · latest ${age(latest.measuredAtMs)}"
}

/** How long a pull keeps the spinner once nothing is syncing. */
private const val PULL_SETTLE_MS = 800L

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
    onBack: () -> Unit,
) {
    // A finished workout is a closed interval; a running one keeps growing.
    val workoutLimit = workout?.let {
        it.startedAtMs..(it.endedAtMs ?: System.currentTimeMillis())
    }
    // Only a running workout has a "now" to jump to or a rest to time.
    val live = workout != null && workout.endedAtMs == null
    Page(workout?.activity ?: "Workout", onBack) {
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
fun WorkoutsScreen(
    workouts: List<WorkoutSummary>,
    onSelect: (WorkoutSummary) -> Unit,
    onBack: () -> Unit,
) {
    Page("Workouts", onBack) {
        if (workouts.isEmpty()) {
            Text(
                "No workouts yet. Start one on the watch; it appears here once " +
                    "the watch has been synced.",
                style = MaterialTheme.typography.bodySmall,
            )
        }
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
 * What the sync is doing, short enough to sit beside the link state.
 *
 * A transfer reports exact bytes; the history walk can only estimate, so the
 * stream count leads and the fraction is only ever a hint alongside it.
 */
private fun syncLabel(snapshot: Snapshot?): String? {
    val sync = snapshot?.sync ?: return null
    val received = sync.transferReceived
    val total = sync.transferTotal
    if (received != null && total != null && total > 0u) {
        return "transfer ${received * 100u / total}%"
    }
    if (snapshot.progress != Progress.SYNCING) return null
    val stream = "syncing ${sync.streamsDone + 1u}/${sync.streamsTotal}"
    val fraction = sync.historyFraction ?: return stream
    return "$stream · ${(fraction * 100).toInt()}%"
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

private fun format(value: Double, style: MetricStyle): String =
    if (style.decimals == 0) value.toInt().toString()
    else String.format(Locale.US, "%.${style.decimals}f", value)

/**
 * Link state and sync phase are different things: the watch can be connected
 * while nothing is syncing. Only the link belongs here; what the sync is doing
 * gets its own chip, so neither has to summarise the other.
 */
private fun linkChip(link: LinkState, progress: Progress?): Pair<String, Tone> = when (link) {
    LinkState.Disconnected -> "not connected" to Tone.Quiet
    LinkState.Connecting -> "connecting" to Tone.Working
    LinkState.Connected -> "subscribing" to Tone.Working
    LinkState.Ready -> when (progress) {
        Progress.NOT_AUTHENTICATED -> "secret refused" to Tone.Bad
        Progress.CONNECTING -> "authenticating" to Tone.Working
        else -> "connected" to Tone.Good
    }
}

private fun formatElapsed(ms: Long): String {
    val total = ms / 1000
    return String.format(Locale.US, "%d:%02d", total / 60, total % 60)
}
