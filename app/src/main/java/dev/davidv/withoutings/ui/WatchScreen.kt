package dev.davidv.withoutings.ui

import android.widget.Toast
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import dev.davidv.withoutings.LinkState
import dev.davidv.withoutings.ui.theme.AppTheme
import uniffi.wpp_ffi.Activity
import uniffi.wpp_ffi.HealthFeature
import uniffi.wpp_ffi.NotificationConfig
import uniffi.wpp_ffi.WatchScreen as WatchScreenEntry
import uniffi.wpp_ffi.WearPosition

private const val ACTIVITY_SLOTS = 8

@Composable
fun WatchTab(
    state: UiState,
    nowMs: Long,
    onWearPosition: (WearPosition) -> Unit,
    onNotifications: (Boolean) -> Unit,
    onSync: () -> Unit,
    onReconnect: () -> Unit,
    onOpenBattery: () -> Unit,
    onOpenUser: () -> Unit,
    onOpenSensors: () -> Unit,
    onOpenActivities: () -> Unit,
    onOpenScreens: () -> Unit,
) {
    Column(
        Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.surface)
            .statusBarsPadding(),
    ) {
        Text(
            "Watch",
            Modifier.padding(horizontal = AppTheme.space.screen).padding(top = 8.dp, bottom = 8.dp),
            style = AppTheme.type.titleDetail,
        )
        Column(
            Modifier
                .fillMaxSize()
                .verticalScroll(rememberScrollState())
                .padding(horizontal = AppTheme.space.screen)
                .padding(top = 6.dp, bottom = 110.dp),
            verticalArrangement = Arrangement.spacedBy(AppTheme.space.blockMetric),
        ) {
            DeviceRow(state, nowMs, onOpenBattery)

            Row(
                Modifier.height(IntrinsicSize.Min),
                horizontalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                FilledAction(
                    "Sync now",
                    Modifier.weight(1f).fillMaxHeight(),
                    shape = AppTheme.pill,
                    container = MaterialTheme.colorScheme.primaryContainer,
                    content = MaterialTheme.colorScheme.onPrimaryContainer,
                    onClick = onSync,
                )
                OutlineAction(
                    "Force reconnect",
                    Modifier.weight(1f).fillMaxHeight(),
                    onClick = onReconnect,
                )
            }

            Eyebrow("worn on", Modifier.padding(top = 4.dp))
            Segmented(
                listOf(
                    WearPosition.LEFT_WRIST to "Left wrist",
                    WearPosition.RIGHT_WRIST to "Right wrist",
                    WearPosition.HIP to "Hip",
                ),
                state.wearPosition,
                onSelect = onWearPosition,
            )

            NotificationRow(state.notifications, onNotifications)

            Eyebrow("what the watch runs", Modifier.padding(top = 8.dp))
            NavRow("Wearer", onClick = onOpenUser)
            RowDivider(inset = 0.dp)
            NavRow("Sensors", onClick = onOpenSensors)
            RowDivider(inset = 0.dp)
            NavRow("Quick-launch activities", onClick = onOpenActivities)
            RowDivider(inset = 0.dp)
            NavRow("Watch screens", onClick = onOpenScreens)
        }
    }
}

@Composable
private fun DeviceRow(state: UiState, nowMs: Long, onOpenBattery: () -> Unit) {
    val newest = state.latest.values.maxOfOrNull { it.atMs }
    val device = state.snapshot?.device
    Column(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(AppTheme.radius.card))
            .background(MaterialTheme.colorScheme.surfaceContainer)
            .padding(horizontal = 15.dp, vertical = 13.dp),
    ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            Text(device?.name ?: "Watch", style = AppTheme.type.rowTitle)
            state.snapshot?.battery?.let {
                BatteryPill(
                    if (it.charging == true) "${it.percent}% ⚡" else "${it.percent}%",
                    onClick = onOpenBattery,
                )
            }
        }
        Spacer(Modifier.height(6.dp))
        val link = when {
            state.link != LinkState.Ready -> "not connected"
            newest != null -> "synced ${freshness(newest, nowMs)}"
            else -> "connected, nothing synced yet"
        }
        Text(
            device?.let { "$link · firmware ${it.firmware}" } ?: link,
            style = AppTheme.type.rowMeta,
            color = AppTheme.colors.onSurfaceTertiary,
        )
    }
}

@Composable
private fun NotificationRow(config: NotificationConfig?, onChange: (Boolean) -> Unit) {
    SettingRow("Show notifications on the watch", "Lowers battery life") {
        AppToggle(config?.accepted == true, enabled = config != null) { onChange(it) }
    }
}

@Composable
fun WatchSensorsScreen(
    features: List<HealthFeature>,
    saveState: SaveState,
    onApply: (List<Pair<UShort, Boolean>>) -> Unit,
    onAcknowledge: () -> Unit,
    onBack: () -> Unit,
) {
    val sensors = features.filter { it.id != NOTIFICATION_FEATURE }
    var edits by remember { mutableStateOf(emptyMap<UShort, Boolean>()) }
    LaunchedEffect(sensors) { if (edits.isEmpty()) edits = emptyMap() }

    DetailScaffold(title = "Sensors", onBack = onBack) {
        Column(Modifier.weight(1f).verticalScroll(rememberScrollState())) {
            if (sensors.isEmpty()) {
                EmptyNote("Nothing yet — reload once the watch is connected.")
            }
            sensors.forEachIndexed { index, feature ->
                if (index > 0) RowDivider(inset = 0.dp)
                val on = edits[feature.id] ?: feature.enabled
                SettingRow(feature.name, rationale = feature.description.takeIf {
                    it.saysSomethingBeyond(feature.name)
                }) {
                    AppToggle(on) { edits = edits + (feature.id to it) }
                }
            }
            Spacer(Modifier.height(16.dp))
        }
        val edited = edits.any { (id, on) -> sensors.first { it.id == id }.enabled != on }
        SaveFooter(
            edited = edited,
            saveState = saveState,
            enabled = edited,
            onAcknowledge = onAcknowledge,
            onSaved = onBack,
        ) {
            onApply(
                edits
                    .filter { (id, on) -> sensors.first { it.id == id }.enabled != on }
                    .map { (id, on) -> id to on }
            )
        }
    }
}


private fun String.saysSomethingBeyond(title: String): Boolean {
    fun squash(text: String) = text.lowercase().filter { it.isLetterOrDigit() }
    val description = squash(this)
    val name = squash(title)
    if (description.isEmpty()) return false
    return !description.contains(name) && !name.contains(description)
}

@Composable
fun WatchActivitiesScreen(
    activities: List<Activity>,
    saveState: SaveState,
    onApply: (List<UInt>) -> Unit,
    onAcknowledge: () -> Unit,
    onBack: () -> Unit,
) {
    var menu by remember { mutableStateOf(activities.filter { it.enabled }) }
    var edited by remember { mutableStateOf(false) }
    LaunchedEffect(activities) { if (!edited) menu = activities.filter { it.enabled } }
    val rest = activities
        .filterNot { candidate -> menu.any { it.id == candidate.id } }
        .sortedBy { it.name }

    DetailScaffold(title = "Quick launch", onBack = onBack) {
        Column(Modifier.weight(1f).verticalScroll(rememberScrollState())) {
            Text(
                "The activities the watch offers from its own menu. Long-press and " +
                    "drag to set the order it lists them in. It holds $ACTIVITY_SLOTS.",
                Modifier.padding(bottom = 8.dp),
                style = AppTheme.type.rowMeta,
                color = AppTheme.colors.onSurfaceTertiary,
            )
            if (menu.isEmpty()) {
                EmptyNote("Nothing on the menu yet — turn one on below.")
            }
            ReorderableColumn(menu, onReorder = { edited = true; menu = it }) { activity, _ ->
                AppCheckbox(true, Modifier.padding(start = 12.dp)) {
                    edited = true
                    menu = menu.filterNot { it.id == activity.id }
                }
                Text(
                    activity.name,
                    Modifier.weight(1f).padding(start = 12.dp),
                    style = AppTheme.type.rowTitle,
                )
            }
            Spacer(Modifier.height(20.dp))
            SectionHeader("All activities")
            rest.forEachIndexed { index, activity ->
                if (index > 0) RowDivider(inset = 0.dp)
                Row(
                    Modifier.fillMaxWidth().padding(vertical = 8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    AppCheckbox(
                        false,
                        Modifier.padding(start = 36.dp),
                        enabled = menu.size < ACTIVITY_SLOTS,
                    ) {
                        edited = true
                        menu = menu + activity.copy(enabled = true)
                    }
                    Text(
                        activity.name,
                        Modifier.weight(1f).padding(start = 12.dp),
                        style = AppTheme.type.rowTitle,
                    )
                }
            }
            Spacer(Modifier.height(16.dp))
        }
        SaveFooter(
            edited = edited,
            saveState = saveState,
            enabled = edited && menu.isNotEmpty(),
            onAcknowledge = onAcknowledge,
            onSaved = onBack,
        ) {
            onApply(menu.map { it.id })
        }
    }
}

@Composable
fun WatchScreensScreen(
    screens: List<WatchScreenEntry>,
    saveState: SaveState,
    onReload: () -> Unit,
    onApply: (ByteArray) -> Unit,
    onAcknowledge: () -> Unit,
    onBack: () -> Unit,
) {
    var order by remember { mutableStateOf(screens) }
    var edited by remember { mutableStateOf(false) }
    LaunchedEffect(screens) { if (!edited) order = screens }

    DetailScaffold(
        title = "Watch screens",
        onBack = onBack,
        trailing = {
            OutlineAction("Reload") {
                edited = false
                onReload()
            }
        },
    ) {
        Column(Modifier.weight(1f).verticalScroll(rememberScrollState())) {
            Text(
                "Long-press and drag to set the order they appear on the watch.",
                Modifier.padding(bottom = 8.dp),
                style = AppTheme.type.rowMeta,
                color = AppTheme.colors.onSurfaceTertiary,
            )
            if (screens.isEmpty()) {
                EmptyNote("No screen list yet — reload once the watch is connected.")
            }
            ReorderableScreens(order, onChange = { edited = true; order = it })
            Spacer(Modifier.height(16.dp))
        }
        SaveFooter(
            edited = edited,
            saveState = saveState,
            enabled = edited && order.any { it.enabled },
            onAcknowledge = onAcknowledge,
            onSaved = onBack,
        ) {
            onApply(order.filter { it.enabled }.map { it.id.toByte() }.toByteArray())
        }
    }
}

/**
 * Every one of these lists is sent whole and replaces what the watch holds —
 * a list read before the watch has answered looks exactly like a list with
 * everything switched off, so a blind press on a page that had not loaded yet
 * would wipe the real one.
 */
@Composable
internal fun SaveFooter(
    edited: Boolean,
    saveState: SaveState,
    enabled: Boolean,
    onAcknowledge: () -> Unit,
    onSaved: () -> Unit,
    onSend: () -> Unit,
) {
    val context = LocalContext.current
    LaunchedEffect(saveState) {
        when (saveState) {
            is SaveState.Saved -> {
                onAcknowledge()
                onSaved()
            }

            is SaveState.Failed -> {
                Toast.makeText(context, saveState.reason, Toast.LENGTH_LONG).show()
                onAcknowledge()
            }

            else -> Unit
        }
    }

    val saving = saveState is SaveState.Saving
    Column(
        Modifier
            .fillMaxWidth()
            .background(
                Brush.verticalGradient(
                    0f to Color.Transparent,
                    0.4f to MaterialTheme.colorScheme.surface,
                )
            )
            .padding(top = 12.dp),
        verticalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        if (edited && !saving) {
            Text(
                "Not saved yet. Changes are local until the watch takes them.",
                style = AppTheme.type.rowMeta,
                color = AppTheme.colors.onSurfaceTertiary,
            )
        }
        FilledAction(
            if (saving) "Saving…" else "Save and close",
            Modifier.fillMaxWidth(),
            shape = AppTheme.pill,
            enabled = enabled && !saving,
            onClick = onSend,
        )
    }
}
