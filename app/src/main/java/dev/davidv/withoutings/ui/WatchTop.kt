package dev.davidv.withoutings.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Battery3Bar
import androidx.compose.material.icons.rounded.BatteryAlert
import androidx.compose.material.icons.rounded.BatteryChargingFull
import androidx.compose.material.icons.rounded.BatteryFull
import androidx.compose.material.icons.automirrored.rounded.BatteryUnknown
import androidx.compose.material.icons.rounded.ChevronRight
import androidx.compose.material.icons.rounded.Schedule
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import dev.davidv.withoutings.ui.theme.AppTheme
import uniffi.wpp_ffi.Battery
import uniffi.wpp_ffi.BatteryState

/** At or under this the battery is shown amber. */
const val BATTERY_LOW_PERCENT = 20u

/** At or under this the battery is shown red, with a note to charge it. */
const val BATTERY_CRITICAL_PERCENT = 10u

private val Amber = Color(0xFFB45F00)
private val AmberContainer = Color(0xFFFFE9CC)
private val Red = Color(0xFFC62828)
private val RedContainer = Color(0xFFFDE2E0)

enum class BatteryUrgency { Unknown, Charging, Normal, Low, Critical }

/** Only ever from a real reading: no reading is [BatteryUrgency.Unknown]. */
fun batteryUrgency(battery: Battery?): BatteryUrgency = when {
    battery == null -> BatteryUrgency.Unknown
    battery.charging == true || battery.state == BatteryState.CHARGING -> BatteryUrgency.Charging
    battery.state == BatteryState.CRITICAL || battery.percent <= BATTERY_CRITICAL_PERCENT -> BatteryUrgency.Critical
    battery.state == BatteryState.LOW || battery.percent <= BATTERY_LOW_PERCENT -> BatteryUrgency.Low
    else -> BatteryUrgency.Normal
}

/** The watch card at the top of the Watch tab: name, link, battery. */
@Composable
fun DeviceCard(
    name: String,
    linkLine: String,
    battery: Battery?,
    nowMs: Long,
    onOpenBattery: () -> Unit,
) {
    Column(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(AppTheme.radius.card))
            .background(MaterialTheme.colorScheme.surfaceContainer)
            .padding(horizontal = 15.dp, vertical = 13.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Column {
            Text(name, style = AppTheme.type.rowTitle, maxLines = 1, overflow = TextOverflow.Ellipsis)
            Text(
                linkLine,
                style = AppTheme.type.rowMeta,
                color = AppTheme.colors.onSurfaceTertiary,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
        BatteryLine(battery, nowMs, onOpenBattery)
    }
}

@Composable
fun BatteryLine(battery: Battery?, nowMs: Long, onClick: () -> Unit) {
    val urgency = batteryUrgency(battery)
    val (icon: ImageVector, tint: Color, background: Color) = when (urgency) {
        BatteryUrgency.Unknown -> Triple(Icons.AutoMirrored.Rounded.BatteryUnknown, AppTheme.colors.onSurfaceDim, Color.Transparent)
        BatteryUrgency.Charging -> Triple(Icons.Rounded.BatteryChargingFull, MaterialTheme.colorScheme.primary, Color.Transparent)
        BatteryUrgency.Normal -> Triple(
            if ((battery?.percent ?: 0u) > 50u) Icons.Rounded.BatteryFull else Icons.Rounded.Battery3Bar,
            MaterialTheme.colorScheme.onSurfaceVariant,
            Color.Transparent,
        )
        BatteryUrgency.Low -> Triple(Icons.Rounded.Battery3Bar, Amber, AmberContainer)
        BatteryUrgency.Critical -> Triple(
            Icons.Rounded.BatteryAlert,
            Red,
            RedContainer,
        )
    }
    val urgent = urgency == BatteryUrgency.Low || urgency == BatteryUrgency.Critical
    val label = when (urgency) {
        BatteryUrgency.Unknown -> "Battery unknown"
        BatteryUrgency.Charging -> "${battery?.percent}% · charging"
        BatteryUrgency.Normal -> "${battery?.percent}%"
        BatteryUrgency.Low -> "${battery?.percent}% · low, charge soon"
        BatteryUrgency.Critical -> "${battery?.percent}% · critical, charge it now"
    }
    Row(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(AppTheme.radius.button))
            .background(background)
            .clickable(onClick = onClick)
            .padding(horizontal = if (urgent) 10.dp else 0.dp, vertical = if (urgent) 8.dp else 2.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Icon(icon, null, Modifier.size(20.dp), tint = tint)
        Text(
            label,
            Modifier.weight(1f),
            style = AppTheme.type.rowMeta.copy(fontWeight = if (urgent) FontWeight.SemiBold else FontWeight.Medium),
            color = if (urgent || urgency == BatteryUrgency.Charging) tint else MaterialTheme.colorScheme.onSurface,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
        if (battery != null) {
            Text(
                age(nowMs - battery.atMs),
                style = AppTheme.type.rowMeta,
                color = AppTheme.colors.onSurfaceTertiary,
                maxLines = 1,
            )
        }
    }
}

/** The way into the time-and-hands screen, big and near the top. */
@Composable
fun TimeAndHandsEntry(onClick: () -> Unit) {
    Row(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(AppTheme.radius.card))
            .background(MaterialTheme.colorScheme.primaryContainer)
            .clickable(onClick = onClick)
            .padding(horizontal = 14.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Box(
            Modifier.size(40.dp).clip(CircleShape).background(MaterialTheme.colorScheme.primary),
            contentAlignment = Alignment.Center,
        ) {
            Icon(Icons.Rounded.Schedule, null, Modifier.size(22.dp), tint = MaterialTheme.colorScheme.onPrimary)
        }
        Column(Modifier.weight(1f)) {
            Text(
                "Set time and align hands",
                style = AppTheme.type.rowTitle.copy(fontWeight = FontWeight.SemiBold),
                color = MaterialTheme.colorScheme.onPrimaryContainer,
            )
            Text(
                "Watch time and hand positions",
                style = AppTheme.type.rowMeta,
                color = MaterialTheme.colorScheme.onPrimaryContainer.copy(alpha = 0.75f),
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
        Icon(Icons.Rounded.ChevronRight, null, tint = MaterialTheme.colorScheme.onPrimaryContainer)
    }
}

private fun age(ms: Long): String {
    val m = (ms / 60_000).coerceAtLeast(0)
    return when {
        m < 1 -> "just now"
        m < 60 -> "$m min ago"
        m < 48 * 60 -> "${m / 60} h ago"
        else -> "${m / (24 * 60)} d ago"
    }
}
