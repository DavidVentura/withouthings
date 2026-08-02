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
import androidx.compose.ui.draw.alpha
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

private val clock = SimpleDateFormat("HH:mm:ss", Locale.getDefault())
private val hourMinute = SimpleDateFormat("HH:mm", Locale.getDefault())
private val stamp = SimpleDateFormat("d MMM HH:mm", Locale.getDefault())
private val day = SimpleDateFormat("d MMM", Locale.getDefault())

private const val STALE_ALPHA = 0.45f

/// The battery has its own pill and HRV only means anything over a night, so
/// neither earns a card here.
private val GRID = listOf(
    MetricStyle.HeartRate,
    MetricStyle.Temperature,
    MetricStyle.Respiratory,
    MetricStyle.Steps,
    MetricStyle.Spo2,
    MetricStyle.Ascent,
    MetricStyle.Calories,
)

/// Running totals the watch resets at local midnight. Steps is not here: it
/// arrives with the day it belongs to and is judged on that instead.
private val DAILY_TOTALS = setOf(
    MetricStyle.Ascent,
    MetricStyle.Calories,
    MetricStyle.Distance,
    MetricStyle.TrackedDuration,
)

/** Local midnight, for deciding whether a reading belongs to today. */
internal fun todayStartMs(): Long = java.util.Calendar.getInstance().apply {
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
    onOpenActivities: () -> Unit,
    onOpenEcgs: () -> Unit,
    onOpenSleep: () -> Unit,
    onOpenMetric: (MetricStyle) -> Unit,
    onOpenSettings: () -> Unit,
    onRefresh: () -> Unit,
) {
    val snapshot = state.snapshot
    // The app syncs on its own; a spinner for that would answer a pull nobody made.
    var pulled by remember { mutableStateOf(false) }
    val syncing = snapshot?.progress == Progress.SYNCING
    LaunchedEffect(pulled, syncing) {
        if (!pulled) return@LaunchedEffect
        if (syncing) return@LaunchedEffect
        // The watch may decline the refresh, so this cannot wait on a sync that never starts.
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
                    // Charging is claimed only from a fresh reading: a stale one
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
                val steps = snapshot?.steps

                MetricStyle.entries.filter { it in GRID }.chunked(2).forEach { row ->
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                        row.forEach { style ->
                            val reading = state.latest[style]
                            // A dash means the watch measured nothing, which is
                            // a different claim from "not recently".
                            val fresh = when (style) {
                                MetricStyle.Steps -> steps != null && steps.dayStartMs >= todayStartMs()
                                // A running total resets at local midnight, so
                                // yesterday's figure is wrong rather than old.
                                in DAILY_TOTALS -> reading != null && reading.atMs >= todayStartMs()
                                else -> reading != null && now - reading.atMs < style.freshFor
                            }
                            val shown = when (style) {
                                MetricStyle.Steps -> steps?.count?.toString()
                                else -> reading?.let { format(it.value, style) }
                            }
                            Stat(
                                style.label,
                                shown ?: "—",
                                when {
                                    style == MetricStyle.Steps && steps != null ->
                                        "${style.unit} · " +
                                            if (fresh) "today" else day.format(Date(steps.dayStartMs))
                                    reading != null -> "${style.unit} · ${age(reading.atMs)}"
                                    else -> style.unit
                                },
                                Modifier.weight(1f).alpha(if (fresh) 1f else STALE_ALPHA),
                            ) { onOpenMetric(style) }
                        }
                        if (row.size == 1) Spacer(Modifier.weight(1f))
                    }
                }

                NavRow("Sleep", "last night", onOpenSleep)
                NavRow("Activities", activitiesDetail(state.activityLog), onOpenActivities)
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

private fun activitiesDetail(entries: List<ActivityEntry>): String {
    val latest = entries.maxByOrNull { it.startedAtMs } ?: return "none recorded"
    return "${entries.size} · latest ${latest.name}, ${age(latest.startedAtMs)}"
}

private fun ecgsDetail(recordings: List<EcgSummary>): String {
    val latest = recordings.maxByOrNull { it.measuredAtMs } ?: return "none recorded"
    return "${recordings.size} · latest ${age(latest.measuredAtMs)}"
}

private const val PULL_SETTLE_MS = 800L

@Composable
fun ActivityScreen(
    state: UiState,
    entry: ActivityEntry?,
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
    val workoutLimit = entry?.let {
        it.startedAtMs..(it.endedAtMs ?: System.currentTimeMillis())
    }
    // Only a workout the watch is recording can still be running; a detected
    // walk is found in windows the watch has already handed over.
    val live = entry is RecordedEntry && entry.endedAtMs == null
    Page(entry?.name ?: "Activity", onBack) {
        Text(
            entry?.let {
                val until = it.endedAtMs ?: System.currentTimeMillis()
                "Start: ${hourMinute.format(Date(it.startedAtMs))} · " +
                    "Duration: ${formatElapsed(until - it.startedAtMs)}" +
                    if (it is DetectedEntry) {
                        " · ${it.detected.steps} steps · ${distance(it.detected.distanceMetres)}"
                    } else {
                        ""
                    }
            } ?: "no activity selected",
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
            bands = state.markers.bands(MaterialTheme.colorScheme.primary.copy(alpha = 0.12f)),
            window = window,
            onWindowChange = onWindowChange,
            axis = 50.0..150.0,
            decimals = 0,
            limit = workoutLimit,
            height = 190.dp,
            lineColor = MaterialTheme.colorScheme.primary,
            gridColor = MaterialTheme.colorScheme.outlineVariant,
            axisColor = MaterialTheme.colorScheme.outline,
            labelColor = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        // Same window as the trace above, so panning or zooming either moves both.
        Text("Temperature", style = MaterialTheme.typography.labelSmall)
        ValueChart(
            points = state.workoutTemp,
            bands = state.markers.bands(MaterialTheme.colorScheme.tertiary.copy(alpha = 0.12f)),
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
        )

        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            if (live) {
                Button(onClick = onToggleStopwatch, modifier = Modifier.weight(1f)) {
                    Text(if (running) "End rest  ${formatElapsed(elapsedMs)}" else "Start rest")
                }
            }
            if (live && !following) {
                Button(onClick = onFollowLive, modifier = Modifier.weight(1f)) {
                    Text("Jump to now")
                }
            }
        }
    }
}

@Composable
fun ActivitiesScreen(
    entries: List<ActivityEntry>,
    onSelect: (ActivityEntry) -> Unit,
    onBack: () -> Unit,
) {
    Page("Activities", onBack) {
        if (entries.isEmpty()) {
            Text(
                "Nothing yet. Workouts started on the watch appear here once it " +
                    "has been synced, and walks are picked out of the activity " +
                    "it counts on its own.",
                style = MaterialTheme.typography.bodySmall,
            )
        }
        LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            items(entries) { entry ->
                Card(Modifier.fillMaxWidth().clickable { onSelect(entry) }) {
                    Column(Modifier.padding(12.dp)) {
                        Text(entry.name, style = MaterialTheme.typography.titleMedium)
                        Text(
                            stamp.format(Date(entry.startedAtMs)),
                            style = MaterialTheme.typography.bodyMedium,
                        )
                        Text(
                            entry.endedAtMs?.let {
                                "duration ${formatElapsed(it - entry.startedAtMs)}"
                            } ?: "in progress",
                            style = MaterialTheme.typography.bodySmall,
                        )
                        if (entry is DetectedEntry) {
                            Text(
                                "detected · ${entry.detected.steps} steps · " +
                                    distance(entry.detected.distanceMetres),
                                style = MaterialTheme.typography.bodySmall,
                            )
                        }
                    }
                }
            }
        }
    }
}

private fun distance(metres: Double): String = if (metres >= 1000) {
    String.format(Locale.getDefault(), "%.2f km", metres / 1000)
} else {
    String.format(Locale.getDefault(), "%.0f m", metres)
}

/** A transfer reports exact bytes; the history walk only estimates, so the count leads. */
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

/** The watch can be connected while nothing is syncing; the sync gets its own chip. */
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
