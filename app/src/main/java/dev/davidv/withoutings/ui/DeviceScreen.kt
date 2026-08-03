package dev.davidv.withoutings.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
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
import uniffi.wpp_ffi.NotificationConfig
import uniffi.wpp_ffi.WearPosition

/** The watch's own configuration: how it is worn, what it measures, what it offers. */
@Composable
fun DeviceSettings(
    wearPosition: WearPosition,
    activities: List<Activity>,
    features: List<HealthFeature>,
    notifications: NotificationConfig?,
    testNotification: UInt?,
    onWearPosition: (WearPosition) -> Unit,
    onActivities: (List<UInt>) -> Unit,
    onFeature: (UShort, Boolean) -> Unit,
    onNotifications: (Boolean) -> Unit,
    onPostTestNotification: () -> Unit,
    onDismissTestNotification: () -> Unit,
    onReload: () -> Unit,
    onReconnect: () -> Unit,
    onSetTime: () -> Unit,
    connected: Boolean,
    onUnpair: () -> Unit,
    onFactoryReset: () -> Unit,
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
            "Phone notifications",
            style = MaterialTheme.typography.labelLarge,
            modifier = Modifier.padding(top = 8.dp),
        )
        Text(
            "The watch reads notifications out of a server this app runs, rather " +
                "than being sent them. This switch is the watch's half; the app " +
                "does not forward the phone's own notifications yet.",
            style = MaterialTheme.typography.bodySmall,
        )
        Card(Modifier.fillMaxWidth()) {
            Row(
                Modifier.fillMaxWidth().padding(12.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column(Modifier.weight(1f)) {
                    Text("Accept notifications", style = MaterialTheme.typography.bodyLarge)
                    Text(
                        when {
                            notifications == null -> "Asking the watch…"
                            notifications.displayed -> "Shown on the watch"
                            else -> "Accepted, but the watch is not showing them"
                        },
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
                // Off and "not answered yet" are different things, and drawing
                // the second as the first states something we do not know.
                Switch(
                    checked = notifications?.accepted == true,
                    enabled = notifications != null,
                    onCheckedChange = onNotifications,
                )
            }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = onPostTestNotification) { Text("Send test notification") }
            Button(
                onClick = onDismissTestNotification,
                enabled = testNotification != null,
            ) { Text("Clear it") }
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

        Unpair(connected = connected, onUnpair = onUnpair, onFactoryReset = onFactoryReset)
    }
}

/**
 * Two ways to put a watch down, and they are not the same thing.
 *
 * Letting go of the key is the irreversible half, not letting go of the watch:
 * a watch that still holds a key this app has kept is taken back on by
 * answering its challenge, which asks nothing of the watch. A watch whose key
 * is gone can only be reached again after it has been erased — so erasing it
 * is what the destructive button does, in that order.
 */
@Composable
private fun Unpair(connected: Boolean, onUnpair: () -> Unit, onFactoryReset: () -> Unit) {
    var asking by remember { mutableStateOf(false) }

    Text(
        "Unpair",
        style = MaterialTheme.typography.labelLarge,
        modifier = Modifier.padding(top = 24.dp),
    )
    Button(onClick = onUnpair) { Text("Unpair") }
    Text(
        "Puts the watch down and keeps its key, so pairing with it again is a " +
            "handshake and nothing else. Nothing on the watch changes and " +
            "nothing collected is lost.",
        style = MaterialTheme.typography.bodySmall,
    )

    Button(
        onClick = { asking = true },
        enabled = connected,
        colors = ButtonDefaults.buttonColors(
            containerColor = MaterialTheme.colorScheme.errorContainer,
            contentColor = MaterialTheme.colorScheme.onErrorContainer,
        ),
        modifier = Modifier.padding(top = 8.dp),
    ) { Text("Factory reset and unpair") }
    Text(
        if (connected) {
            "Erases the watch as well, which is the only way to hand it to " +
                "something else. History already collected stays in this app."
        } else {
            "Needs a live link: the watch has to be told to erase itself " +
                "before its key is dropped, or nothing can reach it again."
        },
        style = MaterialTheme.typography.bodySmall,
        modifier = Modifier.padding(bottom = 24.dp),
    )

    if (!asking) return
    AlertDialog(
        onDismissRequest = { asking = false },
        title = { Text("Factory reset the watch?") },
        text = {
            Text(
                "The watch erases its settings — alarms, screens, goals, wear " +
                    "position — along with the key that pairs it to this app, " +
                    "and reboots. Anything it has recorded but not yet handed " +
                    "over is lost with it. History already synced to this phone " +
                    "is kept.\n\nIf you only want to pair again, plain Unpair " +
                    "does that and costs nothing."
            )
        },
        confirmButton = {
            TextButton(onClick = { asking = false; onFactoryReset() }) {
                Text("Erase and unpair")
            }
        },
        dismissButton = {
            TextButton(onClick = { asking = false }) { Text("Cancel") }
        },
    )
}

private const val ACTIVITY_SLOTS = 8
