package dev.davidv.withoutings.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectDragGesturesAfterLongPress
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Check
import androidx.compose.material.icons.rounded.DragIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.unit.dp
import androidx.compose.ui.zIndex
import dev.davidv.withoutings.ui.theme.AppTheme
import kotlin.math.roundToInt
import uniffi.wpp_ffi.WatchScreen as WatchScreenEntry

private val ROW_HEIGHT = 52.dp

/// A row that is off keeps its place in the order — it is simply not cycled —
/// so it recedes rather than moving to a list of its own.
private const val OFF_ALPHA = 0.55f

/**
 * The watch's screen order, dragged into place.
 *
 * Reordering is local until the watch takes it, which is why the row lifts
 * rather than animating into a new position: nothing here has happened yet.
 */
@Composable
fun ReorderableScreens(
    order: List<WatchScreenEntry>,
    onChange: (List<WatchScreenEntry>) -> Unit,
) {
    var dragging by remember { mutableStateOf<Int?>(null) }
    var dragOffset by remember { mutableStateOf(0f) }
    val rowPx = with(LocalDensity.current) { ROW_HEIGHT.toPx() }

    Column(Modifier.fillMaxWidth()) {
        order.forEachIndexed { index, screen ->
            val held = dragging == index
            Row(
                Modifier
                    .fillMaxWidth()
                    .height(ROW_HEIGHT)
                    .zIndex(if (held) 1f else 0f)
                    .graphicsLayer { translationY = if (held) dragOffset else 0f }
                    .then(
                        if (held) {
                            Modifier
                                .shadow(4.dp, RoundedCornerShape(AppTheme.radius.small))
                                .background(
                                    MaterialTheme.colorScheme.surfaceContainerLowest,
                                    RoundedCornerShape(AppTheme.radius.small),
                                )
                        } else {
                            Modifier
                        }
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
                                    onChange(
                                        order.toMutableList().apply { add(to, removeAt(from)) }
                                    )
                                    dragging = to
                                    dragOffset -= moved * rowPx
                                }
                            },
                        )
                    }
                    .padding(horizontal = 4.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Icon(
                    Icons.Rounded.DragIndicator,
                    null,
                    Modifier.size(20.dp),
                    tint = if (held) {
                        AppTheme.colors.dragHandleActive
                    } else {
                        AppTheme.colors.dragHandle
                    },
                )
                ScreenCheckbox(screen.enabled) { on ->
                    onChange(order.toMutableList().also { it[index] = screen.copy(enabled = on) })
                }
                Text(
                    screen.name,
                    Modifier
                        .weight(1f)
                        .padding(start = 12.dp)
                        .alpha(if (screen.enabled) 1f else OFF_ALPHA),
                    style = AppTheme.type.rowTitle,
                )
                Text(
                    "#${screen.id}",
                    style = AppTheme.type.rowMeta,
                    color = AppTheme.colors.onSurfaceDim,
                )
            }
        }
    }
}

@Composable
private fun ScreenCheckbox(checked: Boolean, onChange: (Boolean) -> Unit) {
    val shape = RoundedCornerShape(AppTheme.radius.checkbox)
    Box(
        Modifier
            .padding(start = 12.dp)
            .size(26.dp)
            .clip(shape)
            .background(if (checked) MaterialTheme.colorScheme.primary else Color.Transparent)
            .then(
                if (checked) {
                    Modifier
                } else {
                    Modifier.border(2.dp, AppTheme.colors.onSurfaceDim, shape)
                }
            )
            .clickable { onChange(!checked) },
        contentAlignment = Alignment.Center,
    ) {
        if (checked) {
            Icon(
                Icons.Rounded.Check,
                null,
                Modifier.size(17.dp),
                tint = MaterialTheme.colorScheme.onPrimary,
            )
        }
    }
}
