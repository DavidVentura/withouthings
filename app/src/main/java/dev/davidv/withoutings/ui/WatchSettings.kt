package dev.davidv.withoutings.ui

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import uniffi.wpp_ffi.Activity
import uniffi.wpp_ffi.HealthFeature
import uniffi.wpp_ffi.WatchScreen
import uniffi.wpp_ffi.WearPosition

private val TABS = listOf("Device", "Screens")

/**
 * Everything that configures the watch, in one place.
 *
 * How it is worn, what it measures and what it shows are all things you set
 * once and then leave alone, so they sit behind one door rather than competing
 * with the day's data for room on the home screen.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun WatchSettingsScreen(
    wearPosition: WearPosition,
    activities: List<Activity>,
    features: List<HealthFeature>,
    screens: List<WatchScreen>,
    onWearPosition: (WearPosition) -> Unit,
    onActivities: (List<UInt>) -> Unit,
    onFeature: (UShort, Boolean) -> Unit,
    onReloadDevice: () -> Unit,
    onReloadScreens: () -> Unit,
    onApplyScreens: (ByteArray) -> Unit,
    onBack: () -> Unit,
) {
    var tab by rememberSaveable { mutableStateOf(0) }
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Watch") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back")
                    }
                },
            )
        },
    ) { inset ->
        Column(Modifier.fillMaxSize().padding(inset)) {
            TabRow(selectedTabIndex = tab) {
                TABS.forEachIndexed { index, label ->
                    Tab(
                        selected = tab == index,
                        onClick = { tab = index },
                        text = { Text(label) },
                    )
                }
            }
            Column(Modifier.fillMaxSize().padding(horizontal = 16.dp, vertical = 12.dp)) {
                when (tab) {
                    0 -> DeviceSettings(
                        wearPosition = wearPosition,
                        activities = activities,
                        features = features,
                        onWearPosition = onWearPosition,
                        onActivities = onActivities,
                        onFeature = onFeature,
                        onReload = onReloadDevice,
                    )

                    else -> ScreenOrderSettings(
                        screens = screens,
                        onRefresh = onReloadScreens,
                        onApply = onApplyScreens,
                    )
                }
            }
        }
    }
}
