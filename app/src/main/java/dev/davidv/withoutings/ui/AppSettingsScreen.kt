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
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import dev.davidv.withoutings.ui.theme.AppTheme

@Composable
fun AppSettingsScreen(
    connected: Boolean,
    listening: Boolean,
    testNotification: UInt?,
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
