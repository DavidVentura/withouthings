package dev.davidv.withoutings.ui

import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.unit.dp
import dev.davidv.withoutings.ui.theme.AppTheme
import uniffi.wpp_ffi.WatchScreen as WatchScreenEntry

private const val OFF_ALPHA = 0.55f

@Composable
fun ReorderableScreens(
    order: List<WatchScreenEntry>,
    onChange: (List<WatchScreenEntry>) -> Unit,
) {
    ReorderableColumn(order, onReorder = onChange) { screen, index ->
        AppCheckbox(screen.enabled, Modifier.padding(start = 12.dp)) { on ->
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
    }
}
