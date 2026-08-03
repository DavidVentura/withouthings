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

/** The watch's quick-launch menu holds eight. */
private const val ACTIVITY_SLOTS = 8

/**
 * The physical device, and nothing else.
 *
 * App-level settings live behind the gear on Now. The split is deliberate:
 * "Watch" means the thing on the wrist, so a setting here always changes
 * something over the air.
 *
 * The three long lists — sensors, the quick-launch menu, the screen order —
 * are pages of their own. Each is edited locally and then sent as one whole,
 * and a list that long inlined here buries everything under it.
 */
@Composable
fun WatchTab(
    state: UiState,
    nowMs: Long,
    onWearPosition: (WearPosition) -> Unit,
    onNotifications: (Boolean) -> Unit,
    onSync: () -> Unit,
    onReconnect: () -> Unit,
    onSetTime: () -> Unit,
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

            // Equal heights: a filled button and an outlined one are built
            // differently and would otherwise sit at two sizes side by side.
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

            SettingRow("Set watch time to current") {
                OutlineAction("Set", onClick = onSetTime)
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

/**
 * What the app knows about the watch, and how recently.
 *
 * Freshness is expressed as behaviour rather than a badge: a sentence, with no
 * cloud or connection iconography anywhere. The pill's own dot is the only one
 * — a second beside the name said the same thing twice.
 */
@Composable
private fun DeviceRow(state: UiState, nowMs: Long, onOpenBattery: () -> Unit) {
    val newest = state.latest.values.maxOfOrNull { it.atMs }
    // Both come from the probe reply, which is kept, so a disconnected watch
    // still names itself and its firmware.
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
            // The watch states its firmware as a bare number and nothing else,
            // so that is what is shown; a dotted version would be invented.
            device?.let { "$link · firmware ${it.firmware}" } ?: link,
            style = AppTheme.type.rowMeta,
            color = AppTheme.colors.onSurfaceTertiary,
        )
    }
}

@Composable
private fun NotificationRow(config: NotificationConfig?, onChange: (Boolean) -> Unit) {
    SettingRow("Show notifications on the watch", "Lowers battery life") {
        // Off and "not answered yet" are different things, and drawing the
        // second as the first states something we do not know — so an
        // unanswered watch leaves the switch disabled rather than off.
        AppToggle(config?.accepted == true, enabled = config != null) { onChange(it) }
    }
}

/**
 * What the watch is told to measure.
 *
 * Each switch goes out on its own, so there is nothing to send at the end.
 */
@Composable
fun WatchSensorsScreen(
    features: List<HealthFeature>,
    saveState: SaveState,
    onApply: (List<Pair<UShort, Boolean>>) -> Unit,
    onAcknowledge: () -> Unit,
    onBack: () -> Unit,
) {
    // The notification tag rides with the switch on the page behind this one;
    // it is not something the watch measures.
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


/**
 * Whether a description is worth the line it takes.
 *
 * The watch's own strings often restate the name — "Respiratory monitoring"
 * described as "Continuous respiratory monitoring" — and a subtitle that only
 * repeats the title trains the reader to stop reading subtitles.
 */
private fun String.saysSomethingBeyond(title: String): Boolean {
    fun squash(text: String) = text.lowercase().filter { it.isLetterOrDigit() }
    val description = squash(this)
    val name = squash(title)
    if (description.isEmpty()) return false
    return !description.contains(name) && !name.contains(description)
}

/** The eight the watch offers from its own menu, sent as one list, in order. */
@Composable
fun WatchActivitiesScreen(
    activities: List<Activity>,
    saveState: SaveState,
    onApply: (List<UInt>) -> Unit,
    onAcknowledge: () -> Unit,
    onBack: () -> Unit,
) {
    // The menu is the chosen ones in the order the watch lists them, which is
    // the order they arrive in, so it is the whole of the local state: being
    // on the menu is being in this list. The rest are the catalogue to add
    // from, by name — the watch knows fifty-odd activities and hunting for one
    // in the order they happen to be numbered is hopeless.
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
                Text(
                    activity.name,
                    Modifier.weight(1f).padding(start = 12.dp),
                    style = AppTheme.type.rowTitle,
                )
                AppToggle(true) {
                    edited = true
                    menu = menu.filterNot { it.id == activity.id }
                }
            }
            Spacer(Modifier.height(20.dp))
            SectionHeader("All activities")
            rest.forEachIndexed { index, activity ->
                if (index > 0) RowDivider(inset = 0.dp)
                Row(
                    Modifier.fillMaxWidth().padding(vertical = 8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(activity.name, Modifier.weight(1f), style = AppTheme.type.rowTitle)
                    // The menu holds eight; refuse the ninth rather than
                    // letting the watch silently drop it.
                    AppToggle(false, enabled = menu.size < ACTIVITY_SLOTS) {
                        edited = true
                        menu = menu + activity.copy(enabled = true)
                    }
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

/**
 * Which screens the watch cycles, and in what order.
 *
 * Screens are numbered, not named: the official app gets its names from the
 * Withings backend, so there is no table to read. Enable one, look at the
 * watch, and write down what it was.
 */
@Composable
fun WatchScreensScreen(
    screens: List<WatchScreenEntry>,
    saveState: SaveState,
    onReload: () -> Unit,
    onApply: (ByteArray) -> Unit,
    onAcknowledge: () -> Unit,
    onBack: () -> Unit,
) {
    // Edited locally, then sent as one list: the watch takes the whole set.
    // Not keyed on `screens`: that list is re-read on every background refresh,
    // which would wipe an edit in progress.
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
 * The bottom of a page that is edited locally and then handed over.
 *
 * The pending state is shown rather than assumed. The button stays down while
 * the watch is being waited on, the page leaves only once the watch has taken
 * the list, and a failure says so and hands the button back rather than
 * closing on a change that never landed.
 *
 * Nothing to save is not the same as saving nothing, so the button is dead
 * until something has actually been changed. Every one of these lists is sent
 * whole and replaces what the watch holds — and a list read before the watch
 * has answered looks exactly like a list with everything switched off, so a
 * blind press on a page that had not loaded yet would wipe the real one.
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
