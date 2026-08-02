package dev.davidv.withoutings

import android.Manifest
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavHostController
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import dev.davidv.withoutings.ble.WatchConnectionService
import dev.davidv.withoutings.ui.IdleScreen
import dev.davidv.withoutings.ui.EcgDetailScreen
import dev.davidv.withoutings.ui.EcgListScreen
import dev.davidv.withoutings.ui.LiveEcgScreen
import dev.davidv.withoutings.ui.MetricScreen
import dev.davidv.withoutings.ui.MetricStyle
import dev.davidv.withoutings.ui.SetupScreen
import dev.davidv.withoutings.ui.SleepScreen
import dev.davidv.withoutings.ui.WatchSettingsScreen
import dev.davidv.withoutings.ui.WatchViewModel
import dev.davidv.withoutings.ui.ActivityScreen
import dev.davidv.withoutings.ui.ActivitiesScreen
import dev.davidv.withoutings.ui.RecordedEntry
import dev.davidv.withoutings.ui.theme.WithoutingsTheme

/// The rate the watch streams a live ECG at, and records one at.
private const val LIVE_ECG_HZ = 300

private object Routes {
    const val HOME = "home"
    const val ACTIVITY = "activity"
    const val ACTIVITIES = "activities"
    const val ECGS = "ecgs"
    const val SLEEP = "sleep"
    const val ECG = "ecg"
    const val LIVE_ECG = "live-ecg"
    const val SETTINGS = "settings"
    const val METRIC = "metric/{metric}"

    fun metric(style: MetricStyle) = "metric/${style.name}"
}

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

    val permissions = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { granted ->
        if (granted[Manifest.permission.BLUETOOTH_CONNECT] == true) {
            WatchConnectionService.start(context)
        }
    }

    LaunchedEffect(configured) {
        if (configured) {
            permissions.launch(
                arrayOf(
                    Manifest.permission.BLUETOOTH_CONNECT,
                    Manifest.permission.BLUETOOTH_SCAN,
                    Manifest.permission.POST_NOTIFICATIONS,
                )
            )
        }
    }

    if (!configured) {
        SetupScreen { mac, secret ->
            settings.mac = mac
            settings.secret = secret
            configured = true
        }
        return
    }

    // The background cadence is deliberately slow, so without this what is on
    // screen would be stale every time you opened the app.
    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_RESUME) model.requestRefresh()
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }

    Navigation(model, rememberNavController())
}

@Composable
private fun Navigation(model: WatchViewModel, nav: NavHostController) {
    val context = LocalContext.current
    val state by model.state.collectAsState()
    val window by model.window.collectAsState()
    val startedAt by model.stopwatchStartedAt.collectAsState()
    val elapsed by model.elapsed.collectAsState()
    val metricWindow by model.metricWindow.collectAsState()
    val selected by model.selectedActivity.collectAsState()
    val ecg by model.ecg.collectAsState()
    val ecgWindow by model.ecgWindow.collectAsState()
    val liveWindow by model.liveWindow.collectAsState()
    val night by model.night.collectAsState()
    val nightWindow by model.nightWindow.collectAsState()

    // A workout is a place you go, not a state the home screen turns into:
    // taking over the start destination left nowhere for Back to go.
    val activeStartedAt = state.snapshot?.activeWorkout?.startedAtMs
    LaunchedEffect(activeStartedAt) {
        if (activeStartedAt != null) nav.navigate(Routes.ACTIVITY)
    }

    // Read the watch's configuration once the link is up, rather than when
    // the settings screen opens. Asked there, the round trip happens while
    // the screen is already showing values it does not have yet.
    LaunchedEffect(state.link) {
        if (state.link == LinkState.Ready) model.requestDeviceConfig()
    }

    // The waveform is only sent while something is showing it, so the screen
    // has to be up for the recording to be captured at all.
    val measuring = state.snapshot?.measuring == true
    LaunchedEffect(measuring) {
        if (measuring) nav.navigate(Routes.LIVE_ECG)
    }

    NavHost(nav, startDestination = Routes.HOME) {
        composable(Routes.HOME) {
            // The charging state is only shown here, so it is only worth
            // asking for here — not behind a workout, which is exactly when
            // the extra traffic would cost something.
            DisposableEffect(Unit) {
                model.watchCharging(true)
                onDispose { model.watchCharging(false) }
            }
            IdleScreen(
                state = state,
                onOpenActivities = { nav.navigate(Routes.ACTIVITIES) },
                onOpenEcgs = { nav.navigate(Routes.ECGS) },
                onOpenSleep = {
                    model.showNight()
                    nav.navigate(Routes.SLEEP)
                },
                onOpenMetric = { nav.navigate(Routes.metric(it)) },
                onOpenSettings = {
                    model.requestDeviceConfig()
                    model.requestScreens()
                    nav.navigate(Routes.SETTINGS)
                },
                onRefresh = { model.requestRefresh() },
            )
        }

        composable(Routes.ACTIVITY) {
            ActivityScreen(
                state = state,
                entry = state.snapshot?.activeWorkout?.let(::RecordedEntry) ?: selected,
                window = state.hrWindow,
                elapsedMs = elapsed,
                running = startedAt != null,
                following = window == null,
                onWindowChange = { model.zoom(it) },
                onFollowLive = { model.followLive() },
                onToggleStopwatch = { model.toggleStopwatch() },
                onBack = { nav.popBackStack() },
            )
        }

        composable(Routes.METRIC) { entry ->
            val style = entry.arguments?.getString("metric")
                ?.let { runCatching { MetricStyle.valueOf(it) }.getOrNull() }
                ?: MetricStyle.HeartRate
            LaunchedEffect(style) { model.showMetric(style) }
            val now = System.currentTimeMillis()
            MetricScreen(
                style = style,
                points = state.metric,
                markers = state.charging,
                window = metricWindow ?: ((now - style.defaultSpan)..now),
                onWindowChange = { model.metricZoom(it) },
                onRange = { model.metricRangeSpan(it) },
                onBack = { nav.popBackStack() },
            )
        }

        composable(Routes.ACTIVITIES) {
            ActivitiesScreen(
                entries = state.activityLog,
                onSelect = { entry ->
                    model.showActivity(entry)
                    nav.navigate(Routes.ACTIVITY)
                },
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

        composable(Routes.SLEEP) {
            SleepScreen(
                night = night,
                window = nightWindow ?: 0L..1L,
                onWindowChange = { model.nightZoom(it) },
                onShift = { model.shiftNight(it) },
                onBack = { nav.popBackStack() },
            )
        }

        composable(Routes.ECGS) {
            EcgListScreen(
                recordings = state.ecgs,
                onSelect = {
                    model.showEcg(it.id)
                    nav.navigate(Routes.ECG)
                },
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

        composable(Routes.SETTINGS) {
            val testNotification by model.testNotification.collectAsState()
            WatchSettingsScreen(
                wearPosition = state.wearPosition,
                activities = state.activities,
                features = state.features,
                screens = state.screens,
                notifications = state.notifications,
                testNotification = testNotification,
                onWearPosition = { model.setWearPosition(it) },
                onActivities = { model.setActivities(it) },
                onFeature = { id, on -> model.setFeature(id, on) },
                onNotifications = { model.setNotifications(it) },
                onPostTestNotification = { model.postTestNotification() },
                onDismissTestNotification = { model.dismissTestNotification() },
                onReloadDevice = { model.requestDeviceConfig() },
                onReconnect = { WatchConnectionService.reconnect(context) },
                onSetTime = { model.setWatchTime() },
                onReloadScreens = { model.requestScreens() },
                onApplyScreens = { model.applyScreens(it) },
                onBack = { nav.popBackStack() },
            )
        }
    }
}
