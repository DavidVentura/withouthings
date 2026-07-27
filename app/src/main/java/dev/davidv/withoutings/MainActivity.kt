package dev.davidv.withoutings

import android.Manifest
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavHostController
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import dev.davidv.withoutings.ble.WatchConnectionService
import dev.davidv.withoutings.ui.IdleScreen
import dev.davidv.withoutings.ui.DeviceScreen
import dev.davidv.withoutings.ui.MetricScreen
import dev.davidv.withoutings.ui.MetricStyle
import dev.davidv.withoutings.ui.ScreensScreen
import dev.davidv.withoutings.ui.SetupScreen
import dev.davidv.withoutings.ui.WatchViewModel
import dev.davidv.withoutings.ui.WorkoutScreen
import dev.davidv.withoutings.ui.WorkoutsScreen
import dev.davidv.withoutings.ui.theme.WithoutingsTheme

private object Routes {
    const val HOME = "home"
    const val WORKOUTS = "workouts"
    const val SCREENS = "screens"
    const val DEVICE = "device"
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

    Navigation(model, rememberNavController())
}

@Composable
private fun Navigation(model: WatchViewModel, nav: NavHostController) {
    val state by model.state.collectAsState()
    val window by model.window.collectAsState()
    val startedAt by model.stopwatchStartedAt.collectAsState()
    val elapsed by model.elapsed.collectAsState()
    val metricWindow by model.metricWindow.collectAsState()

    NavHost(nav, startDestination = Routes.HOME) {
        composable(Routes.HOME) {
            // A running workout takes over: the 1 Hz trace only exists while it
            // lasts, so it is what you want in front of you.
            val active = state.snapshot?.activeWorkout
            if (active != null || window != null) {
                WorkoutScreen(
                    state = state,
                    window = window ?: (active!!.startedAtMs..System.currentTimeMillis()),
                    elapsedMs = elapsed,
                    running = startedAt != null,
                    following = window == null,
                    onWindowChange = { model.zoom(it) },
                    onFollowLive = { model.zoom(null) },
                    onToggleStopwatch = { model.toggleStopwatch() },
                )
            } else {
                IdleScreen(
                    state = state,
                    onOpenWorkouts = { nav.navigate(Routes.WORKOUTS) },
                    onOpenScreens = {
                        model.requestScreens()
                        nav.navigate(Routes.SCREENS)
                    },
                    onOpenMetric = { nav.navigate(Routes.metric(it)) },
                    onOpenDevice = {
                        model.requestDeviceConfig()
                        nav.navigate(Routes.DEVICE)
                    },
                    onRefresh = { model.requestRefresh() },
                )
            }
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
                window = metricWindow ?: ((now - style.defaultSpan)..now),
                onWindowChange = { model.metricZoom(it) },
                onRange = { model.metricRangeSpan(it) },
            )
        }

        composable(Routes.WORKOUTS) {
            WorkoutsScreen(
                workouts = state.workouts,
                onSelect = { workout ->
                    model.zoom(
                        workout.startedAtMs..(workout.endedAtMs ?: System.currentTimeMillis())
                    )
                    nav.popBackStack()
                },
            )
        }

        composable(Routes.DEVICE) {
            DeviceScreen(
                wearPosition = state.wearPosition,
                activities = state.activities,
                features = state.features,
                onWearPosition = { model.setWearPosition(it) },
                onActivities = { model.setActivities(it) },
                onFeature = { id, on -> model.setFeature(id, on) },
                onReload = { model.requestDeviceConfig() },
            )
        }

        composable(Routes.SCREENS) {
            ScreensScreen(
                screens = state.screens,
                onRefresh = { model.requestScreens() },
                onApply = { model.applyScreens(it) },
            )
        }
    }
}
