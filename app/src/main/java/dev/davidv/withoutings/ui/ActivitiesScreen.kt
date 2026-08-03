package dev.davidv.withoutings.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.horizontalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.DirectionsWalk
import androidx.compose.material.icons.rounded.FitnessCenter
import androidx.compose.material.icons.rounded.MonitorHeart
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.unit.dp
import dev.davidv.withoutings.ui.theme.AppTheme
import uniffi.wpp_ffi.EcgRhythm
import uniffi.wpp_ffi.EcgSummary

@Composable
fun ActivitiesScreen(
    entries: List<ActivityEntry>,
    recordings: List<EcgSummary>,
    nowMs: Long,
    onSelect: (ActivityEntry) -> Unit,
    onSelectEcg: (EcgSummary) -> Unit,
) {
    var filter by remember { mutableStateOf<String?>(null) }

    val items = (
        entries.map { Item.Activity(it) } + recordings.map { Item.Recording(it) }
        ).sortedByDescending { it.atMs }
    val kinds = items.map { it.kind }.distinct().sorted()
    val shown = items.filter { filter == null || it.kind == filter }

    Column(
        Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.surface)
            .statusBarsPadding(),
    ) {
        Text(
            "Activities",
            Modifier.padding(horizontal = AppTheme.space.screen).padding(top = 8.dp, bottom = 10.dp),
            style = AppTheme.type.titleDetail,
        )
        Row(
            Modifier
                .horizontalScroll(rememberScrollState())
                .padding(horizontal = AppTheme.space.screen),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Chip("All", filter == null) { filter = null }
            kinds.forEach { kind ->
                Chip(kind, filter == kind) { filter = kind }
            }
        }
        Spacer(Modifier.height(10.dp))

        if (shown.isEmpty()) {
            EmptyNote(
                "Nothing yet. Workouts started on the watch appear here once it " +
                    "has been synced, walks are picked out of the activity it " +
                    "counts on its own, and ECGs arrive with the recording.",
                Modifier.padding(horizontal = AppTheme.space.screen),
            )
            return@Column
        }

        val byDay = shown.groupBy { dayStart(it.atMs) }
        LazyColumn(
            Modifier.weight(1f).navigationBarsPadding(),
            contentPadding = androidx.compose.foundation.layout.PaddingValues(
                start = AppTheme.space.screen,
                end = AppTheme.space.screen,
                bottom = 24.dp,
            ),
        ) {
            byDay.forEach { (day, dayItems) ->
                item(key = "head-$day") {
                    Eyebrow(
                        dayHeading(day, dayItems, nowMs),
                        Modifier.padding(top = 14.dp, bottom = 4.dp),
                        style = AppTheme.type.eyebrowLarge,
                    )
                }
                itemsIndexed(dayItems) { index, item ->
                    if (index > 0) RowDivider()
                    when (item) {
                        is Item.Activity -> EntityRow(
                            icon = item.icon,
                            title = item.entry.name,
                            meta = entryMeta(item.entry, nowMs, withDay = false),
                            accent = item.entry is RecordedEntry,
                        ) { onSelect(item.entry) }

                        is Item.Recording -> EntityRow(
                            icon = Icons.Rounded.MonitorHeart,
                            title = "Electrocardiogram",
                            meta = ecgMeta(item.summary),
                            accent = false,
                        ) { onSelectEcg(item.summary) }
                    }
                }
            }
        }
    }
}

private fun ecgMeta(summary: EcgSummary): String = listOfNotNull(
    clock(summary.measuredAtMs),
    "${summary.seconds.toInt()} s".takeIf { summary.seconds.toInt() != STANDARD_ECG_SECONDS },
    summary.heartRate?.let { "$it bpm" },
    when (summary.rhythm) {
        EcgRhythm.NO_AFIB -> "no AFib"
        EcgRhythm.AFIB -> "AFib"
        EcgRhythm.INCONCLUSIVE -> "inconclusive"
        EcgRhythm.POOR_RECORDING -> "poor recording"
        EcgRhythm.RATE_OUT_OF_RANGE -> "rate out of range"
        EcgRhythm.NO_RESULT, null -> null
    },
).joinToString(" · ")

private const val STANDARD_ECG_SECONDS = 30

private fun dayHeading(dayMs: Long, items: List<Item>, nowMs: Long): String {
    val steps = items
        .filterIsInstance<Item.Activity>()
        .mapNotNull { it.entry as? DetectedEntry }
        .sumOf { it.detected.steps }
    val name = dayName(dayMs, nowMs)
    if (steps <= 0) {
        return "$name · ${items.size} recorded"
    }
    return "$name · ${grouped(steps)} steps"
}

private sealed interface Item {
    val atMs: Long
    val kind: String

    data class Activity(val entry: ActivityEntry) : Item {
        override val atMs = entry.startedAtMs
        override val kind = entry.name
        val icon: ImageVector
            get() = if (entry is RecordedEntry) {
                Icons.Rounded.FitnessCenter
            } else {
                Icons.AutoMirrored.Rounded.DirectionsWalk
            }
    }

    data class Recording(val summary: EcgSummary) : Item {
        override val atMs = summary.measuredAtMs
        override val kind = "ECG"
    }
}
