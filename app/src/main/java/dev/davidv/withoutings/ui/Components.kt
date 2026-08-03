package dev.davidv.withoutings.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectDragGesturesAfterLongPress
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.RowScope
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.ArrowBack
import androidx.compose.material.icons.rounded.Check
import androidx.compose.material.icons.rounded.ChevronLeft
import androidx.compose.material.icons.rounded.ChevronRight
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
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.zIndex
import dev.davidv.withoutings.ui.theme.AppTheme
import kotlin.math.roundToInt

private val TOUCH = 40.dp

@Composable
private fun Modifier.tap(onClick: (() -> Unit)?): Modifier =
    if (onClick == null) this else clickable { onClick() }

@Composable
fun HomeScaffold(
    title: String,
    subtitle: String,
    trailing: @Composable RowScope.() -> Unit = {},
    content: @Composable ColumnScope.() -> Unit,
) {
    val space = AppTheme.space
    Column(
        Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.surface)
            .statusBarsPadding(),
    ) {
        Row(
            Modifier
                .fillMaxWidth()
                .padding(start = space.screen, end = space.screen, top = 10.dp, bottom = 12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(Modifier.weight(1f)) {
                Text(title, style = AppTheme.type.titleHome)
                Text(
                    subtitle,
                    style = AppTheme.type.bodyLarge,
                    color = AppTheme.colors.onSurfaceTertiary,
                )
            }
            trailing()
        }
        Column(
            Modifier.weight(1f).padding(horizontal = space.screen).padding(bottom = 12.dp),
            verticalArrangement = Arrangement.spacedBy(space.block),
            content = content,
        )
    }
}

@Composable
fun DetailScaffold(
    title: String,
    onBack: () -> Unit,
    subtitle: String? = null,
    gap: Dp = AppTheme.space.blockMetric,
    trailing: @Composable RowScope.() -> Unit = {},
    content: @Composable ColumnScope.() -> Unit,
) {
    val space = AppTheme.space
    Column(
        Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.surface)
            .statusBarsPadding()
            .navigationBarsPadding(),
    ) {
        Row(
            Modifier
                .fillMaxWidth()
                .padding(horizontal = space.screen - 8.dp)
                .padding(top = 6.dp, bottom = 10.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            GlyphButton(Icons.AutoMirrored.Rounded.ArrowBack, "Back", onClick = onBack)
            Column(Modifier.weight(1f).padding(start = 2.dp)) {
                Text(title, style = AppTheme.type.titleDetail)
                if (subtitle != null) {
                    Text(
                        subtitle,
                        style = AppTheme.type.titleMeta,
                        color = AppTheme.colors.onSurfaceTertiary,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
            }
            trailing()
        }
        Column(
            Modifier.weight(1f).padding(horizontal = space.screen).padding(bottom = 12.dp),
            verticalArrangement = Arrangement.spacedBy(gap),
            content = content,
        )
    }
}

@Composable
fun GlyphButton(
    icon: ImageVector,
    description: String,
    enabled: Boolean = true,
    size: Dp = 22.dp,
    onClick: () -> Unit,
) {
    Box(
        Modifier
            .size(TOUCH)
            .clip(CircleShape)
            .tap(if (enabled) onClick else null),
        contentAlignment = Alignment.Center,
    ) {
        Icon(
            icon,
            description,
            Modifier.size(size),
            tint = if (enabled) {
                MaterialTheme.colorScheme.onSurfaceVariant
            } else {
                AppTheme.colors.onSurfaceDisabled
            },
        )
    }
}

@Composable
fun DayStepper(canGoForward: Boolean, onStep: (Int) -> Unit) {
    GlyphButton(Icons.Rounded.ChevronLeft, "Earlier") { onStep(-1) }
    GlyphButton(Icons.Rounded.ChevronRight, "Later", enabled = canGoForward) { onStep(1) }
}

@Composable
fun BatteryPill(
    label: String,
    dotColor: Color = MaterialTheme.colorScheme.primary,
    onClick: (() -> Unit)? = null,
) {
    Row(
        Modifier
            .clip(AppTheme.pill)
            .background(MaterialTheme.colorScheme.surfaceContainerHigh)
            .tap(onClick)
            .padding(start = 9.dp, end = 11.dp, top = 6.dp, bottom = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        Box(Modifier.size(7.dp).clip(CircleShape).background(dotColor))
        Text(
            label,
            style = AppTheme.type.rowMeta.copy(
                fontWeight = androidx.compose.ui.text.font.FontWeight.Medium,
            ),
            color = MaterialTheme.colorScheme.onSurface,
        )
    }
}

@Composable
fun Eyebrow(
    text: String,
    modifier: Modifier = Modifier,
    color: Color = AppTheme.colors.onSurfaceTertiary,
    style: TextStyle = AppTheme.type.eyebrow,
) {
    Text(text.uppercase(), modifier, style = style, color = color)
}

@Composable
fun SectionHeader(title: String, action: String? = null, onAction: (() -> Unit)? = null) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        Text(
            title,
            Modifier.weight(1f),
            style = AppTheme.type.sectionTitle,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        if (action != null && onAction != null) {
            Text(
                action,
                Modifier
                    .clip(AppTheme.pill)
                    .tap(onAction)
                    .padding(horizontal = 14.dp, vertical = 10.dp),
                style = AppTheme.type.buttonLabel.copy(
                    fontSize = AppTheme.type.chipLabel.fontSize,
                ),
                color = MaterialTheme.colorScheme.primary,
            )
        }
    }
}

@Composable
fun Tile(
    modifier: Modifier = Modifier,
    shape: Shape = RoundedCornerShape(AppTheme.radius.card),
    color: Color = MaterialTheme.colorScheme.surfaceContainer,
    onClick: (() -> Unit)? = null,
    content: @Composable ColumnScope.() -> Unit,
) {
    Column(
        modifier.clip(shape).background(color).tap(onClick).padding(
            horizontal = AppTheme.space.tileHorizontal,
            vertical = AppTheme.space.tile,
        ),
        content = content,
    )
}

@Composable
fun AccentCard(
    modifier: Modifier = Modifier,
    shape: Shape = RoundedCornerShape(AppTheme.radius.accent),
    onClick: (() -> Unit)? = null,
    content: @Composable ColumnScope.() -> Unit,
) {
    Column(
        modifier
            .clip(shape)
            .background(MaterialTheme.colorScheme.primaryContainer)
            .tap(onClick)
            .padding(horizontal = 17.dp, vertical = 14.dp),
        content = content,
    )
}

@Composable
fun ChartCard(
    modifier: Modifier = Modifier,
    shape: Shape = RoundedCornerShape(AppTheme.radius.card),
    content: @Composable ColumnScope.() -> Unit,
) {
    Column(
        modifier
            .clip(shape)
            .background(MaterialTheme.colorScheme.surfaceContainerLowest)
            .border(1.dp, MaterialTheme.colorScheme.surfaceContainerHigh, shape)
            .padding(horizontal = 8.dp, vertical = 10.dp),
        content = content,
    )
}

@Composable
fun MetricTile(
    icon: ImageVector,
    label: String,
    value: String,
    unit: String,
    context: String,
    modifier: Modifier = Modifier,
    onClick: (() -> Unit)? = null,
) {
    Column(
        modifier
            .clip(RoundedCornerShape(AppTheme.radius.tile))
            .background(MaterialTheme.colorScheme.surfaceContainer)
            .tap(onClick)
            .padding(horizontal = AppTheme.space.tileHorizontal, vertical = AppTheme.space.tile),
    ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(5.dp),
        ) {
            Icon(
                icon,
                null,
                Modifier.size(16.dp),
                tint = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Text(
                label,
                style = AppTheme.type.tileLabel,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Spacer(Modifier.height(6.dp))
        ValueWithUnit(value, unit, AppTheme.type.tileValue)
        Spacer(Modifier.weight(1f).height(6.dp))
        Text(
            context,
            style = AppTheme.type.tileContext,
            color = AppTheme.colors.onSurfaceTertiary,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
    }
}

@Composable
fun StatTile(
    eyebrow: String,
    value: String,
    unit: String,
    footer: String,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier
            .clip(RoundedCornerShape(AppTheme.radius.card))
            .background(MaterialTheme.colorScheme.surfaceContainer)
            .padding(horizontal = AppTheme.space.leading, vertical = 11.dp),
    ) {
        Eyebrow(eyebrow)
        Spacer(Modifier.height(4.dp))
        ValueWithUnit(value, unit, AppTheme.type.statValue)
        Spacer(Modifier.height(3.dp))
        Text(
            footer,
            style = AppTheme.type.tileContext,
            color = AppTheme.colors.onSurfaceTertiary,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
    }
}

@Composable
fun ValueWithUnit(
    value: String,
    unit: String,
    style: TextStyle,
    color: Color = MaterialTheme.colorScheme.onSurface,
    unitColor: Color = AppTheme.colors.onSurfaceTertiary,
    unitStyle: TextStyle = AppTheme.type.unit,
) {
    Row(verticalAlignment = Alignment.Bottom) {
        Text(value, style = style, color = color, maxLines = 1)
        if (unit.isNotEmpty()) {
            Text(
                unit,
                Modifier.padding(start = 4.dp, bottom = 2.dp),
                style = unitStyle,
                color = unitColor,
                maxLines = 1,
            )
        }
    }
}

@Composable
fun EntityRow(
    icon: ImageVector,
    title: String,
    meta: String,
    modifier: Modifier = Modifier,
    accent: Boolean = true,
    trailing: @Composable RowScope.() -> Unit = { RowChevron() },
    onClick: (() -> Unit)? = null,
) {
    Row(
        modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(AppTheme.radius.button))
            .tap(onClick)
            .padding(vertical = AppTheme.space.listRow),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            Modifier
                .size(42.dp)
                .clip(RoundedCornerShape(AppTheme.radius.small))
                .background(
                    if (accent) {
                        MaterialTheme.colorScheme.primaryContainer
                    } else {
                        MaterialTheme.colorScheme.surfaceContainerHigh
                    }
                ),
            contentAlignment = Alignment.Center,
        ) {
            Icon(
                icon,
                null,
                Modifier.size(20.dp),
                tint = if (accent) {
                    MaterialTheme.colorScheme.onPrimaryContainer
                } else {
                    MaterialTheme.colorScheme.onSurfaceVariant
                },
            )
        }
        Column(Modifier.weight(1f).padding(start = AppTheme.space.leading)) {
            Text(title, style = AppTheme.type.rowTitle, maxLines = 1, overflow = TextOverflow.Ellipsis)
            Text(
                meta,
                style = AppTheme.type.rowMeta,
                color = AppTheme.colors.onSurfaceTertiary,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
        trailing()
    }
}

@Composable
fun RowChevron() {
    Icon(
        Icons.Rounded.ChevronRight,
        null,
        Modifier.size(20.dp),
        tint = AppTheme.colors.onSurfaceDim,
    )
}

@Composable
fun RowDivider(inset: Dp = AppTheme.space.dividerInset) {
    Box(
        Modifier
            .fillMaxWidth()
            .padding(start = inset)
            .height(1.dp)
            .background(MaterialTheme.colorScheme.surfaceContainerHigh)
    )
}

@Composable
fun <T> ChipRow(
    options: List<Pair<T, String>>,
    selected: T?,
    modifier: Modifier = Modifier,
    gap: Dp = 7.dp,
    onSelect: (T) -> Unit,
) {
    Row(modifier, horizontalArrangement = Arrangement.spacedBy(gap)) {
        options.forEach { (value, label) ->
            Chip(label, value == selected) { onSelect(value) }
        }
    }
}

@Composable
fun Chip(label: String, selected: Boolean, onClick: () -> Unit) {
    val shape = RoundedCornerShape(AppTheme.radius.chip)
    Box(
        Modifier
            .clip(shape)
            .background(
                if (selected) MaterialTheme.colorScheme.primaryContainer else Color.Transparent
            )
            .then(
                if (selected) Modifier else Modifier.border(1.dp, MaterialTheme.colorScheme.outline, shape)
            )
            .clickable { onClick() }
            .padding(horizontal = AppTheme.space.leading, vertical = 7.dp),
    ) {
        Text(
            label,
            style = AppTheme.type.chipLabel,
            color = if (selected) {
                MaterialTheme.colorScheme.onPrimaryContainer
            } else {
                MaterialTheme.colorScheme.onSurfaceVariant
            },
        )
    }
}

@Composable
fun FilledAction(
    label: String,
    modifier: Modifier = Modifier,
    icon: ImageVector? = null,
    enabled: Boolean = true,
    shape: Shape = RoundedCornerShape(AppTheme.radius.card),
    container: Color = MaterialTheme.colorScheme.primary,
    content: Color = MaterialTheme.colorScheme.onPrimary,
    onClick: () -> Unit,
) {
    Row(
        modifier
            .clip(shape)
            .background(if (enabled) container else MaterialTheme.colorScheme.surfaceContainerHigh)
            .tap(if (enabled) onClick else null)
            .padding(vertical = 15.dp, horizontal = 16.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp, Alignment.CenterHorizontally),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        val tint = if (enabled) content else AppTheme.colors.onSurfaceDim
        if (icon != null) Icon(icon, null, Modifier.size(20.dp), tint = tint)
        Text(label, style = AppTheme.type.buttonLabel, color = tint)
    }
}

@Composable
fun OutlineAction(
    label: String,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    onClick: () -> Unit,
) {
    val shape = AppTheme.pill
    Box(
        modifier
            .clip(shape)
            .border(1.dp, MaterialTheme.colorScheme.outline, shape)
            .tap(if (enabled) onClick else null)
            .padding(vertical = 11.dp, horizontal = 14.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            label,
            style = AppTheme.type.buttonLabel.copy(fontSize = AppTheme.type.chipLabel.fontSize),
            color = if (enabled) {
                MaterialTheme.colorScheme.primary
            } else {
                AppTheme.colors.onSurfaceDim
            },
        )
    }
}

@Composable
fun AppToggle(checked: Boolean, enabled: Boolean = true, onChange: (Boolean) -> Unit) {
    val colors = AppTheme.colors
    Box(
        Modifier
            .size(width = 48.dp, height = 28.dp)
            .clip(AppTheme.pill)
            .background(
                when {
                    !enabled -> MaterialTheme.colorScheme.surfaceContainerHigh
                    checked -> MaterialTheme.colorScheme.primary
                    else -> colors.track
                }
            )
            .tap(if (enabled) ({ onChange(!checked) }) else null)
            .padding(4.dp),
        contentAlignment = if (checked) Alignment.CenterEnd else Alignment.CenterStart,
    ) {
        Box(
            Modifier
                .size(20.dp)
                .clip(CircleShape)
                .background(
                    if (checked) MaterialTheme.colorScheme.onPrimary else colors.toggleOffKnob
                )
                .then(
                    if (checked) {
                        Modifier
                    } else {
                        Modifier.border(1.dp, colors.toggleOffKnobRing, CircleShape)
                    }
                )
        )
    }
}

@Composable
fun <T> Segmented(
    options: List<Pair<T, String>>,
    selected: T?,
    modifier: Modifier = Modifier,
    onSelect: (T) -> Unit,
) {
    val radius = AppTheme.radius.pill
    Row(modifier.fillMaxWidth()) {
        options.forEachIndexed { index, (value, label) ->
            val first = index == 0
            val last = index == options.lastIndex
            val shape = RoundedCornerShape(
                topStart = if (first) radius else 0.dp,
                bottomStart = if (first) radius else 0.dp,
                topEnd = if (last) radius else 0.dp,
                bottomEnd = if (last) radius else 0.dp,
            )
            val chosen = value == selected
            Box(
                Modifier
                    .weight(1f)
                    .clip(shape)
                    .background(
                        if (chosen) MaterialTheme.colorScheme.primaryContainer else Color.Transparent
                    )
                    .border(
                        1.dp,
                        if (chosen) {
                            MaterialTheme.colorScheme.primary
                        } else {
                            MaterialTheme.colorScheme.outline
                        },
                        shape,
                    )
                    .clickable { onSelect(value) }
                    .padding(vertical = 10.dp),
                contentAlignment = Alignment.Center,
            ) {
                Text(
                    label,
                    style = AppTheme.type.sectionTitle,
                    color = if (chosen) {
                        MaterialTheme.colorScheme.onPrimaryContainer
                    } else {
                        MaterialTheme.colorScheme.onSurfaceVariant
                    },
                )
            }
        }
    }
}

@Composable
fun TrackBar(
    fraction: Float,
    modifier: Modifier = Modifier,
    height: Dp = 6.dp,
    color: Color = MaterialTheme.colorScheme.primary,
) {
    Box(
        modifier
            .height(height)
            .clip(AppTheme.pill)
            .background(AppTheme.colors.track),
    ) {
        Box(
            Modifier
                .fillMaxWidth(fraction.coerceIn(0f, 1f))
                .fillMaxHeight()
                .clip(AppTheme.pill)
                .background(color)
        )
    }
}

@Composable
fun LegendSwatch(color: Color, label: String) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(5.dp),
    ) {
        Box(Modifier.size(9.dp).clip(RoundedCornerShape(2.dp)).background(color))
        Text(
            label,
            style = AppTheme.type.axisSmall,
            color = AppTheme.colors.onSurfaceDim,
        )
    }
}

@Composable
fun EmptyNote(text: String, modifier: Modifier = Modifier) {
    Text(
        text,
        modifier,
        style = AppTheme.type.body,
        color = AppTheme.colors.onSurfaceTertiary,
    )
}

val REORDER_ROW_HEIGHT = 52.dp

@Composable
fun <T> ReorderableColumn(
    order: List<T>,
    onReorder: (List<T>) -> Unit,
    modifier: Modifier = Modifier,
    row: @Composable RowScope.(item: T, index: Int) -> Unit,
) {
    var dragging by remember { mutableStateOf<Int?>(null) }
    var dragOffset by remember { mutableStateOf(0f) }
    val rowPx = with(LocalDensity.current) { REORDER_ROW_HEIGHT.toPx() }

    Column(modifier.fillMaxWidth()) {
        order.forEachIndexed { index, item ->
            val held = dragging == index
            Row(
                Modifier
                    .fillMaxWidth()
                    .height(REORDER_ROW_HEIGHT)
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
                                    onReorder(
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
                row(item, index)
            }
        }
    }
}

@Composable
fun AppCheckbox(
    checked: Boolean,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    onChange: (Boolean) -> Unit,
) {
    val shape = RoundedCornerShape(AppTheme.radius.checkbox)
    Box(
        modifier
            .size(26.dp)
            .clip(shape)
            .background(if (checked) MaterialTheme.colorScheme.primary else Color.Transparent)
            .then(
                if (checked) {
                    Modifier
                } else {
                    Modifier.border(
                        2.dp,
                        if (enabled) {
                            AppTheme.colors.onSurfaceDim
                        } else {
                            MaterialTheme.colorScheme.surfaceContainerHigh
                        },
                        shape,
                    )
                }
            )
            .clickable(enabled = enabled) { onChange(!checked) },
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

@Composable
fun NavRow(title: String, modifier: Modifier = Modifier, onClick: () -> Unit) {
    Row(
        modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(AppTheme.radius.button))
            .clickable { onClick() }
            .padding(vertical = 14.dp, horizontal = 2.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(title, Modifier.weight(1f), style = AppTheme.type.rowTitle)
        RowChevron()
    }
}

@Composable
fun SettingRow(
    title: String,
    rationale: String? = null,
    modifier: Modifier = Modifier,
    control: @Composable () -> Unit,
) {
    Row(
        modifier.fillMaxWidth().padding(vertical = 9.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f).padding(end = 12.dp)) {
            Text(title, style = AppTheme.type.rowTitle)
            if (rationale != null) {
                Text(
                    rationale,
                    style = AppTheme.type.rowMeta,
                    color = AppTheme.colors.onSurfaceTertiary,
                )
            }
        }
        control()
    }
}

