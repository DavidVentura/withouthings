package dev.davidv.withoutings.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectDragGesturesAfterLongPress
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
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
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.unit.dp
import kotlin.math.roundToInt
import uniffi.wpp_ffi.WatchScreen

private val ROW_HEIGHT = 56.dp

/**
 * Which screens the watch cycles, and in what order.
 *
 * Screens are numbered, not named: the official app gets its names from the
 * Withings backend, so there is no table to read. Enable one, look at the
 * watch, and write down what it was.
 */
@Composable
fun ScreensScreen(
    screens: List<WatchScreen>,
    onRefresh: () -> Unit,
    onApply: (ByteArray) -> Unit,
) {
    // Edited locally, then sent as one list: the watch takes the whole set.
    // Not keyed on `screens`: that list is re-read on every background refresh,
    // which would wipe an edit in progress.
    var order by remember { mutableStateOf(screens) }
    var edited by remember { mutableStateOf(false) }
    LaunchedEffect(screens) { if (!edited) order = screens }
    var dragging by remember { mutableStateOf<Int?>(null) }
    var dragOffset by remember { mutableStateOf(0f) }
    val rowPx = with(LocalDensity.current) { ROW_HEIGHT.toPx() }

    Column(
        Modifier.fillMaxSize().statusBarsPadding().padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Text("Watch screens", style = MaterialTheme.typography.headlineMedium)
        Text(
            "Long-press to drag. Enabled screens appear on the watch in this order.",
            style = MaterialTheme.typography.bodySmall,
        )

        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = { edited = false; onRefresh() }) { Text("Reload") }
            Button(
                onClick = {
                    edited = false
                    onApply(order.filter { it.enabled }.map { it.id.toByte() }.toByteArray())
                },
                enabled = order.isNotEmpty(),
            ) { Text("Send to watch") }
        }

        if (screens.isEmpty()) {
            Text("No screen list yet — reload once the watch is connected.")
            return@Column
        }

        Column(Modifier.verticalScroll(rememberScrollState())) {
            order.forEachIndexed { index, screen ->
                val held = dragging == index
                Row(
                    Modifier
                        .fillMaxWidth()
                        .height(ROW_HEIGHT)
                        .graphicsLayer { translationY = if (held) dragOffset else 0f }
                        .background(
                            if (held) MaterialTheme.colorScheme.surfaceVariant
                            else MaterialTheme.colorScheme.surface
                        )
                        .pointerInput(order) {
                            detectDragGesturesAfterLongPress(
                                onDragStart = { dragging = index; dragOffset = 0f },
                                onDragEnd = { dragging = null; dragOffset = 0f },
                                onDragCancel = { dragging = null; dragOffset = 0f },
                                onDrag = { change, amount ->
                                    change.consume()
                                    dragOffset += amount.y
                                    val moved = (dragOffset / rowPx).roundToInt()
                                    val from = dragging ?: return@detectDragGesturesAfterLongPress
                                    val to = (from + moved).coerceIn(0, order.size - 1)
                                    if (to != from) {
                                        edited = true
                                        order = order.toMutableList().apply {
                                            add(to, removeAt(from))
                                        }
                                        dragging = to
                                        dragOffset -= moved * rowPx
                                    }
                                },
                            )
                        }
                        .padding(horizontal = 4.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Checkbox(
                        checked = screen.enabled,
                        onCheckedChange = { on ->
                            edited = true
                            order = order.toMutableList().also {
                                it[index] = screen.copy(enabled = on)
                            }
                        },
                    )
                    Text(
                        screen.name,
                        Modifier.alpha(if (screen.enabled) 1f else 0.5f),
                        style = MaterialTheme.typography.bodyLarge,
                    )
                    Text(
                        "  #${screen.id}",
                        style = MaterialTheme.typography.labelSmall,
                        modifier = Modifier.alpha(0.4f),
                    )
                }
            }
        }
    }
}
