package dev.davidv.withoutings.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Check
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import dev.davidv.withoutings.ui.theme.AppTheme
import uniffi.wpp_ffi.WatchScreen as WatchScreenEntry

/// A row that is off keeps its place in the order — it is simply not cycled —
/// so it recedes rather than moving to a list of its own.
private const val OFF_ALPHA = 0.55f

/** The watch's screen order, dragged into place. */
@Composable
fun ReorderableScreens(
    order: List<WatchScreenEntry>,
    onChange: (List<WatchScreenEntry>) -> Unit,
) {
    ReorderableColumn(order, onReorder = onChange) { screen, index ->
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
