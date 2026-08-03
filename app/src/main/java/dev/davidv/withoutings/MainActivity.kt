package dev.davidv.withoutings

import android.Manifest
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavHostController
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import dev.davidv.withoutings.ble.PairingSession
import dev.davidv.withoutings.ble.PairingStage
import dev.davidv.withoutings.ble.WatchConnectionService
import dev.davidv.withoutings.ui.ActivitiesScreen
import dev.davidv.withoutings.ui.ActivityDetailScreen
import dev.davidv.withoutings.ui.AppSettingsScreen
import dev.davidv.withoutings.ui.BottomNav
import dev.davidv.withoutings.ui.EcgDetailScreen
import dev.davidv.withoutings.ui.LIVE_ECG_HZ
import dev.davidv.withoutings.ui.LiveEcgScreen
import dev.davidv.withoutings.ui.LiveWorkoutScreen
import dev.davidv.withoutings.ui.MetricScreen
import dev.davidv.withoutings.ui.MetricStyle
import dev.davidv.withoutings.ui.NowScreen
import dev.davidv.withoutings.ui.PairingScreen
import dev.davidv.withoutings.ui.RecordedEntry
import dev.davidv.withoutings.ui.SleepScreen
import dev.davidv.withoutings.ui.Tab
import dev.davidv.withoutings.ui.TodayScreen
import dev.davidv.withoutings.ui.WatchActivitiesScreen
import dev.davidv.withoutings.ui.WatchScreensScreen
import dev.davidv.withoutings.ui.WatchSensorsScreen
import dev.davidv.withoutings.ui.WatchTab
import dev.davidv.withoutings.ui.WatchUserScreen
import dev.davidv.withoutings.ui.WatchViewModel
import dev.davidv.withoutings.ui.theme.WithoutingsTheme
import kotlinx.coroutines.delay

private object Routes {
    const val ACTIVITY = "activity"
    const val LIVE = "live"
    const val SLEEP = "sleep"
    const val ECG = "ecg"
    const val LIVE_ECG = "live-ecg"
    const val SETTINGS = "settings"
    const val METRIC = "metric"
    const val WATCH_USER = "watch-user"
    const val WATCH_SENSORS = "watch-sensors"
    const val WATCH_ACTIVITIES = "watch-activities"
    const val WATCH_SCREENS = "watch-screens"
}

private const val CLOCK_TICK_MS = 1_000L

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { WithoutingsTheme { App() } }
    }
}

@Composable
private fun App(model: WatchViewModel = viewModel()) {
    val context = LocalContext.current
    val settings = remember { Settings(context) }
    var configured by remember { mutableStateOf(settings.configured) }
    var radio by remember { mutableStateOf(false) }

    val permissions = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { granted ->
        radio = granted[Manifest.permission.BLUETOOTH_CONNECT] == true &&
            granted[Manifest.permission.BLUETOOTH_SCAN] == true
    }

    LaunchedEffect(Unit) {
        permissions.launch(
            arrayOf(
                Manifest.permission.BLUETOOTH_CONNECT,
                Manifest.permission.BLUETOOTH_SCAN,
                Manifest.permission.POST_NOTIFICATIONS,
            )
        )
    }

    LaunchedEffect(configured, radio) {
        if (configured && radio) WatchConnectionService.start(context)
    }

    if (!configured) {
        Pairing(settings, radio) { configured = true }
        return
    }

    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_RESUME) model.requestRefresh()
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }

    Navigation(
        model,
        rememberNavController(),
        onUnpair = {
            settings.forgetWatch()
            WatchConnectionService.stop(context)
            configured = false
        },
        onFactoryReset = {
            val mac = settings.mac
            model.unpair {
                if (mac != null) settings.forgetWatchAndKey(mac)
                WatchConnectionService.stop(context)
                configured = false
            }
        },
    )
}

@Composable
private fun Pairing(settings: Settings, radio: Boolean, onPaired: () -> Unit) {
    val context = LocalContext.current
    val devices by PairingSession.devices.collectAsState()
    val stage by PairingSession.stage.collectAsState()

    LaunchedEffect(radio) { if (radio) PairingSession.startScan(context) }
    DisposableEffect(Unit) {
        onDispose {
            PairingSession.stopScan(context)
            PairingSession.reset()
        }
    }
    LaunchedEffect(stage) {
        val paired = stage as? PairingStage.Paired ?: return@LaunchedEffect
        settings.select(paired.mac, paired.secret)
        onPaired()
    }

    PairingScreen(
        devices = devices,
        stage = stage,
        known = settings.knownWatches.size,
        onPair = {
            PairingSession.pair(context, it, settings.accountId, settings.knownWatches)
        },
        onRescan = { PairingSession.startScan(context) },
    )
}

@Composable
private fun Navigation(
    model: WatchViewModel,
    nav: NavHostController,
    onUnpair: () -> Unit,
    onFactoryReset: () -> Unit,
) {
    val context = LocalContext.current
    val settings = remember { Settings(context) }
    val state by model.state.collectAsState()
    val window by model.window.collectAsState()
    val startedAt by model.stopwatchStartedAt.collectAsState()
    val elapsed by model.elapsed.collectAsState()
    val metricStyle by model.metricStyle.collectAsState()
    val metricWindow by model.metricWindow.collectAsState()
    val selected by model.selectedActivity.collectAsState()
    val selectedTotals by model.selectedTotals.collectAsState()
    val ecg by model.ecg.collectAsState()
    val ecgWindow by model.ecgWindow.collectAsState()
    val liveWindow by model.liveWindow.collectAsState()
    val night by model.night.collectAsState()
    val saving by model.save.collectAsState()
    val nightWindow by model.nightWindow.collectAsState()

    var nowMs by remember { mutableLongStateOf(System.currentTimeMillis()) }
    LaunchedEffect(Unit) {
        while (true) {
            nowMs = System.currentTimeMillis()
            delay(CLOCK_TICK_MS)
        }
    }

    val activeStartedAt = state.snapshot?.activeWorkout?.startedAtMs
    LaunchedEffect(activeStartedAt) {
        val onLive = nav.currentBackStackEntry?.destination?.route == Routes.LIVE
        when {
            activeStartedAt != null && !onLive -> nav.navigate(Routes.LIVE)
            activeStartedAt == null && onLive -> nav.popBackStack()
        }
    }

    LaunchedEffect(state.link) {
        if (state.link == LinkState.Ready) {
            model.requestDeviceConfig()
            model.requestScreens()
        }
    }

    val measuring = state.snapshot?.measuring == true
    LaunchedEffect(measuring) {
        if (measuring) nav.navigate(Routes.LIVE_ECG)
    }

    val entry by nav.currentBackStackEntryAsState()
    val tab = Tab.entries.firstOrNull { it.route == entry?.destination?.route }

    fun selectTab(target: Tab) {
        nav.navigate(target.route) {
            popUpTo(Tab.Now.route) { this.saveState = true }
            launchSingleTop = true
            restoreState = true
        }
    }

    Column(Modifier.fillMaxSize()) {
        NavHost(
            nav,
            startDestination = Tab.Now.route,
            modifier = Modifier.weight(1f),
        ) {
            composable(Tab.Now.route) {
                DisposableEffect(Unit) {
                    model.watchCharging(true)
                    onDispose { model.watchCharging(false) }
                }
                NowScreen(
                    state = state,
                    nowMs = nowMs,
                    onOpenMetric = {
                        model.showMetric(it)
                        nav.navigate(Routes.METRIC)
                    },
                    onOpenSleep = {
                        model.showNight()
                        nav.navigate(Routes.SLEEP)
                    },
                    onOpenActivities = { selectTab(Tab.Activity) },
                    onOpenActivity = {
                        model.showActivity(it)
                        nav.navigate(Routes.ACTIVITY)
                    },
                    onOpenLive = { nav.navigate(Routes.LIVE) },
                    onStartWorkout = { model.startWorkout(it) },
                    onOpenSettings = { nav.navigate(Routes.SETTINGS) },
                )
            }

            composable(Tab.Today.route) {
                TodayScreen(
                    state = state,
                    nowMs = nowMs,
                    onOpenActivity = {
                        model.showActivity(it)
                        nav.navigate(Routes.ACTIVITY)
                    },
                    onOpenSleep = {
                        model.showNight()
                        nav.navigate(Routes.SLEEP)
                    },
                )
            }

            composable(Tab.Activity.route) {
                ActivitiesScreen(
                    entries = state.activityLog,
                    recordings = state.ecgs,
                    dailySteps = state.dailySteps,
                    nowMs = nowMs,
                    onSelect = {
                        model.showActivity(it)
                        nav.navigate(Routes.ACTIVITY)
                    },
                    onSelectEcg = {
                        model.showEcg(it.id)
                        nav.navigate(Routes.ECG)
                    },
                )
            }

            composable(Tab.Watch.route) {
                WatchTab(
                    state = state,
                    nowMs = nowMs,
                    onWearPosition = { model.setWearPosition(it) },
                    onNotifications = {
                        settings.notifications = it
                        model.setNotifications(it)
                        WatchConnectionService.setNotifications(context, it)
                    },
                    onSync = { model.requestDeviceConfig(); model.requestRefresh() },
                    onReconnect = { WatchConnectionService.reconnect(context) },
                    onOpenBattery = {
                        model.showMetric(MetricStyle.Battery)
                        nav.navigate(Routes.METRIC)
                    },
                    onOpenUser = { nav.navigate(Routes.WATCH_USER) },
                    onOpenSensors = {
                        model.requestDeviceConfig()
                        nav.navigate(Routes.WATCH_SENSORS)
                    },
                    onOpenActivities = {
                        model.requestDeviceConfig()
                        nav.navigate(Routes.WATCH_ACTIVITIES)
                    },
                    onOpenScreens = {
                        model.requestScreens()
                        nav.navigate(Routes.WATCH_SCREENS)
                    },
                )
            }

            composable(Routes.WATCH_USER) {
                WatchUserScreen(
                    user = state.snapshot?.user,
                    saveState = saving,
                    onApply = { birth, weight, height ->
                        model.applyUser(birth, weight, height)
                    },
                    onAcknowledge = { model.acknowledgeSave() },
                    onBack = { nav.popBackStack() },
                )
            }

            composable(Routes.WATCH_SENSORS) {
                WatchSensorsScreen(
                    features = state.features,
                    saveState = saving,
                    onApply = { model.applyFeatures(it) },
                    onAcknowledge = { model.acknowledgeSave() },
                    onBack = { nav.popBackStack() },
                )
            }

            composable(Routes.WATCH_ACTIVITIES) {
                WatchActivitiesScreen(
                    activities = state.activities,
                    saveState = saving,
                    onApply = { model.applyActivities(it) },
                    onAcknowledge = { model.acknowledgeSave() },
                    onBack = { nav.popBackStack() },
                )
            }

            composable(Routes.WATCH_SCREENS) {
                WatchScreensScreen(
                    screens = state.screens,
                    saveState = saving,
                    onReload = { model.requestScreens() },
                    onApply = { model.applyScreens(it) },
                    onAcknowledge = { model.acknowledgeSave() },
                    onBack = { nav.popBackStack() },
                )
            }

            composable(Routes.LIVE) {
                LiveWorkoutScreen(
                    state = state,
                    workout = state.snapshot?.activeWorkout,
                    window = state.hrWindow,
                    nowMs = nowMs,
                    restElapsedMs = elapsed,
                    resting = startedAt != null,
                    following = window == null,
                    onWindowChange = { model.zoom(it) },
                    onFollowLive = { model.followLive() },
                    onToggleRest = { model.toggleStopwatch() },
                    onStopWorkout = { model.stopWorkout() },
                    onBack = { nav.popBackStack() },
                )
            }

            composable(Routes.ACTIVITY) {
                ActivityDetailScreen(
                    state = state,
                    entry = selected,
                    window = state.hrWindow,
                    nowMs = nowMs,
                    totals = selectedTotals,
                    onWindowChange = { model.zoom(it) },
                    onDelete = { model.deleteActivity(it); nav.popBackStack() },
                    onBack = { nav.popBackStack() },
                )
            }

            composable(Routes.METRIC) {
                MetricScreen(
                    style = metricStyle,
                    state = state,
                    window = metricWindow
                        ?: ((nowMs - metricStyle.defaultSpan)..nowMs),
                    nowMs = nowMs,
                    onWindowChange = { model.metricZoom(it) },
                    onRange = { model.metricRangeSpan(it) },
                    onSelectStyle = { model.showMetric(it) },
                    onOpenSession = { session ->
                        state.activityLog
                            .firstOrNull { it.startedAtMs == session.span.fromMs }
                            ?.let {
                                model.showActivity(it)
                                nav.navigate(Routes.ACTIVITY)
                            }
                    },
                    onBack = { nav.popBackStack() },
                )
            }

            composable(Routes.SLEEP) {
                SleepScreen(
                    night = night,
                    window = nightWindow ?: 0L..1L,
                    nowMs = nowMs,
                    onShift = { model.shiftNight(it) },
                    onBack = { nav.popBackStack() },
                )
            }

            composable(Routes.ECG) {
                EcgDetailScreen(
                    recording = ecg,
                    window = ecgWindow ?: 0L..1L,
                    onWindowChange = { model.ecgZoom(it) },
                    onBack = { nav.popBackStack() },
                )
            }

            composable(Routes.LIVE_ECG) {
                LiveEcgScreen(
                    millivolts = state.liveEcg,
                    samplingHz = LIVE_ECG_HZ,
                    recording = state.snapshot?.measuring == true,
                    window = liveWindow,
                    onWindowChange = { model.liveEcgZoom(it) },
                    onBack = { nav.popBackStack() },
                )
            }

            composable(Routes.SETTINGS) {
                val testNotification by model.testNotification.collectAsState()
                val listening by WatchRepository.listening.collectAsState()
                AppSettingsScreen(
                    listening = listening,
                    connected = state.link == LinkState.Ready,
                    testNotification = testNotification,
                    onPostTestNotification = { model.postTestNotification() },
                    onDismissTestNotification = { model.dismissTestNotification() },
                    onUnpair = onUnpair,
                    onFactoryReset = onFactoryReset,
                    onBack = { nav.popBackStack() },
                )
            }
        }

        if (tab != null) {
            BottomNav(tab) { target -> if (target != tab) selectTab(target) }
        }
    }
}
