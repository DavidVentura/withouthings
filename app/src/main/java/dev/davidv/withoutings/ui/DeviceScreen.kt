package dev.davidv.withoutings.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import uniffi.wpp_ffi.Activity
import uniffi.wpp_ffi.HealthFeature
import uniffi.wpp_ffi.WearPosition

/** The watch's own configuration: how it is worn, what it measures, what it offers. */
@Composable
fun DeviceSettings(
    wearPosition: WearPosition,
    activities: List<Activity>,
    features: List<HealthFeature>,
    onWearPosition: (WearPosition) -> Unit,
    onActivities: (List<UInt>) -> Unit,
    onFeature: (UShort, Boolean) -> Unit,
    onReload: () -> Unit,
    onReconnect: () -> Unit,
    onSetTime: () -> Unit,
) {
    // Edited locally, sent as one list: the watch takes the whole menu.
    var menu by remember { mutableStateOf(activities) }
    var edited by remember { mutableStateOf(false) }
    LaunchedEffect(activities) { if (!edited) menu = activities }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = { edited = false; onReload() }) { Text("Reload from watch") }
            Button(onClick = onReconnect) { Text("Force reconnect") }
        }
        Text(
            "The link is held by a background service, so closing the app does " +
                "not restart it. This does.",
            style = MaterialTheme.typography.bodySmall,
        )

        Button(onClick = onSetTime) { Text("Set watch time") }
        Text(
            "Sets the watch's clock to this phone's, with its time zone and the " +
                "next daylight-saving change.",
            style = MaterialTheme.typography.bodySmall,
        )

        Text("Worn on", style = MaterialTheme.typography.labelLarge)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            listOf(
                WearPosition.LEFT_WRIST to "Left wrist",
                WearPosition.RIGHT_WRIST to "Right wrist",
                WearPosition.HIP to "Hip",
            ).forEach { (position, label) ->
                FilterChip(
                    selected = wearPosition == position,
                    onClick = { onWearPosition(position) },
                    label = { Text(label) },
                )
            }
        }

        Text(
            "Health features",
            style = MaterialTheme.typography.labelLarge,
            modifier = Modifier.padding(top = 8.dp),
        )
        Text(
            "The watch cannot be asked what it has enabled, so this is what this " +
                "app last sent it, starting from what the official app had set.",
            style = MaterialTheme.typography.bodySmall,
        )
        features.forEach { feature ->
            Card(Modifier.fillMaxWidth()) {
                Row(
                    Modifier.fillMaxWidth().padding(12.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Column(Modifier.weight(1f)) {
                        Text(feature.name, style = MaterialTheme.typography.bodyLarge)
                        Text(feature.description, style = MaterialTheme.typography.bodySmall)
                    }
                    Switch(
                        checked = feature.enabled,
                        onCheckedChange = { onFeature(feature.id, it) },
                    )
                }
            }
        }

        Text(
            "Quick-launch activities",
            style = MaterialTheme.typography.labelLarge,
            modifier = Modifier.padding(top = 8.dp),
        )
        val chosen = menu.count { it.enabled }
        Text(
            "$chosen of $ACTIVITY_SLOTS chosen, in the order the watch lists them.",
            style = MaterialTheme.typography.bodySmall,
        )
        Button(
            onClick = {
                edited = false
                onActivities(menu.filter { it.enabled }.map { it.id })
            },
        ) { Text("Send menu to watch") }

        menu.forEachIndexed { index, activity ->
            Row(
                Modifier.fillMaxWidth().padding(vertical = 2.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Switch(
                    checked = activity.enabled,
                    // The menu holds eight; refuse the ninth rather than
                    // letting the watch silently drop it.
                    enabled = activity.enabled || chosen < ACTIVITY_SLOTS,
                    onCheckedChange = { on ->
                        edited = true
                        menu = menu.toMutableList().also {
                            it[index] = activity.copy(enabled = on)
                        }
                    },
                )
                Text(
                    activity.name,
                    Modifier.padding(start = 8.dp),
                    style = MaterialTheme.typography.bodyMedium,
                )
            }
        }
    }
}

private const val ACTIVITY_SLOTS = 8
