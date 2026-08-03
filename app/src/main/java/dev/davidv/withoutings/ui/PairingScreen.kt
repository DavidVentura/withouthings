package dev.davidv.withoutings.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import dev.davidv.withoutings.ble.Discovered
import dev.davidv.withoutings.ble.PairingStage

/**
 * Finding a watch and claiming it.
 *
 * Every device in range is listed rather than only the ones that look like a
 * watch: the address is random and rotates, the advertisement carries no
 * service UUID, and a name filter that misses is indistinguishable from a
 * watch that is not there. What can be recognised is marked and sorted first.
 */
@Composable
fun PairingScreen(
    devices: List<Discovered>,
    stage: PairingStage,
    known: Int,
    onPair: (String) -> Unit,
    onRescan: () -> Unit,
) {
    Column(
        Modifier.fillMaxSize().statusBarsPadding().padding(horizontal = 24.dp, vertical = 16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("Pair a watch", style = MaterialTheme.typography.headlineSmall)

        when (stage) {
            is PairingStage.Scanning -> Text(
                "The watch advertises rarely — minutes can pass between " +
                    "packets — so give it time. Press its button to wake it." +
                    if (known > 0) {
                        " A watch this app already has a key for is taken back " +
                            "on without being changed."
                    } else "",
                style = MaterialTheme.typography.bodySmall,
            )

            is PairingStage.AlreadyAssociated -> Status(
                "That watch belongs to something else",
                "It challenged with an identity this app has no key for. " +
                    "Factory reset it from whatever paired with it, then scan " +
                    "again.",
                busy = false,
            )

            is PairingStage.Failed -> Status("Pairing failed", stage.reason, busy = false)
            is PairingStage.Connecting -> Status("Connecting…", null, busy = true)
            is PairingStage.Probing -> Status("Asking whether it is free…", null, busy = true)
            is PairingStage.Associating -> Status("Handing over the key…", null, busy = true)
            is PairingStage.Readopting -> Status(
                "Already have this one's key — proving it…", null, busy = true,
            )
            is PairingStage.Paired -> Status("Paired", stage.mac, busy = false)
        }

        Button(onClick = onRescan) { Text("Scan again") }

        Text(
            "${devices.size} device${if (devices.size == 1) "" else "s"} in range",
            style = MaterialTheme.typography.labelLarge,
        )
        LazyColumn(
            Modifier.fillMaxSize(),
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            items(devices, key = { it.address }) { device ->
                Card(
                    Modifier.fillMaxWidth().clickable { onPair(device.address) },
                ) {
                    Row(
                        Modifier.fillMaxWidth().padding(12.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Column(Modifier.weight(1f)) {
                            Text(
                                device.name ?: "(no name)",
                                style = MaterialTheme.typography.bodyLarge,
                            )
                            Text(device.address, style = MaterialTheme.typography.bodySmall)
                        }
                        Text(
                            device.rssi?.let { "$it dBm" } ?: "bonded",
                            style = MaterialTheme.typography.labelMedium,
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun Status(title: String, detail: String?, busy: Boolean) {
    Row(
        Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        if (busy) CircularProgressIndicator(Modifier.padding(4.dp))
        Column {
            Text(title, style = MaterialTheme.typography.bodyLarge)
            if (detail != null) Text(detail, style = MaterialTheme.typography.bodySmall)
        }
    }
}
