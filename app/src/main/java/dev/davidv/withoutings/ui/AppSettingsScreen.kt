package dev.davidv.withoutings.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import dev.davidv.withoutings.Settings
import dev.davidv.withoutings.ui.theme.AppTheme

@Composable
fun AppSettingsScreen(
    connected: Boolean,
    listening: Boolean,
    testNotification: UInt?,
    routes: RouteSettings,
    onPostTestNotification: () -> Unit,
    onDismissTestNotification: () -> Unit,
    onUnpair: () -> Unit,
    onFactoryReset: () -> Unit,
    onBack: () -> Unit,
) {
    var asking by remember { mutableStateOf(false) }

    DetailScaffold(title = "Settings", onBack = onBack) {
        Column(
            Modifier.fillMaxSize().verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(AppTheme.space.blockMetric),
        ) {
            RouteSection(routes)

            Eyebrow("diagnostics")
            Text(
                if (listening) {
                    "Posts a notification of this app's own, to exercise the path " +
                        "without reading the phone's real ones. The watch keeps it on " +
                        "screen until it is cleared."
                } else {
                    "The watch has not subscribed to the notification server yet. " +
                        "It does that a few seconds after connecting, and anything " +
                        "sent before then is discarded."
                },
                style = AppTheme.type.body,
                color = AppTheme.colors.onSurfaceTertiary,
            )
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlineAction(
                    "Send test",
                    Modifier.weight(1f),
                    enabled = listening,
                    onClick = onPostTestNotification,
                )
                OutlineAction(
                    "Clear it",
                    Modifier.weight(1f),
                    enabled = testNotification != null,
                    onClick = onDismissTestNotification,
                )
            }

            Eyebrow("this watch", Modifier.padding(top = 12.dp))
            Text(
                "Unpairing puts the watch down and keeps its key, so pairing with " +
                    "it again is a handshake and nothing else. Nothing on the watch " +
                    "changes and nothing collected is lost.",
                style = AppTheme.type.body,
                color = AppTheme.colors.onSurfaceTertiary,
            )
            OutlineAction("Unpair", Modifier.fillMaxWidth(), onClick = onUnpair)

            Text(
                if (connected) {
                    "A factory reset erases the watch as well, which is the only " +
                        "way to hand it to something else. History already " +
                        "collected stays in this app."
                } else {
                    "A factory reset needs a live link: the watch has to be told " +
                        "to erase itself before its key is dropped, or nothing can " +
                        "reach it again."
                },
                Modifier.padding(top = 8.dp),
                style = AppTheme.type.body,
                color = AppTheme.colors.onSurfaceTertiary,
            )
            OutlineAction(
                "Factory reset and unpair",
                Modifier.fillMaxWidth(),
                enabled = connected,
            ) { asking = true }
            Spacer(Modifier.height(24.dp))
        }
    }

    if (!asking) return
    AlertDialog(
        onDismissRequest = { asking = false },
        containerColor = MaterialTheme.colorScheme.surfaceContainer,
        title = { Text("Factory reset the watch?") },
        text = {
            Text(
                "The watch erases its settings — alarms, screens, goals, wear " +
                    "position — along with the key that pairs it to this app, and " +
                    "reboots. Anything it has recorded but not yet handed over is " +
                    "lost with it. History already synced to this phone is kept." +
                    "\n\nIf you only want to pair again, plain Unpair does that " +
                    "and costs nothing."
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

/**
 * The watch has no receiver of its own, so recording a route means the phone's
 * own position. Both switches are off until asked for: one costs a permission
 * and a good deal of battery, the other is the only thing this app ever sends
 * off the phone.
 */
data class RouteSettings(
    val recording: Boolean,
    val permitted: Boolean,
    val tiles: Boolean,
    val tileUrl: String,
    val onRecording: (Boolean) -> Unit,
    val onTiles: (Boolean) -> Unit,
    val onTileUrl: (String) -> Unit,
)

@Composable
private fun RouteSection(routes: RouteSettings) {
    Eyebrow("routes")
    Text(
        "Cycling, running and the other activities that cover ground can be " +
            "recorded with the phone's position, which is the only way to get " +
            "a route: the watch has no receiver. Nothing is recorded outside " +
            "a session, and nothing at all for activities on the spot.",
        style = AppTheme.type.body,
        color = AppTheme.colors.onSurfaceTertiary,
    )
    SettingRow(
        "Record routes",
        if (routes.recording && !routes.permitted) {
            "Waiting on the location permission"
        } else {
            "Uses the receiver for as long as the session lasts"
        },
    ) {
        Switch(checked = routes.recording, onCheckedChange = routes.onRecording)
    }

    SettingRow(
        "Show map tiles",
        "Off, a route is drawn on its own. On, each tile is fetched from the " +
            "server below, which learns where the route went.",
    ) {
        Switch(checked = routes.tiles, onCheckedChange = routes.onTiles)
    }
    if (routes.tiles) {
        var typed by remember(routes.tileUrl) { mutableStateOf(routes.tileUrl) }
        OutlinedTextField(
            value = typed,
            onValueChange = { typed = it },
            modifier = Modifier.fillMaxWidth(),
            label = { Text("Tile URL") },
            singleLine = true,
            keyboardOptions = KeyboardOptions(
                keyboardType = KeyboardType.Uri,
                imeAction = ImeAction.Done,
            ),
        )
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            OutlineAction(
                "Use it",
                Modifier.weight(1f),
                enabled = typed != routes.tileUrl && typed.isNotBlank(),
            ) { routes.onTileUrl(typed) }
            OutlineAction(
                "Back to OpenStreetMap",
                Modifier.weight(1f),
                enabled = routes.tileUrl != Settings.DEFAULT_TILE_URL,
            ) { routes.onTileUrl(Settings.DEFAULT_TILE_URL) }
        }
    }
}
