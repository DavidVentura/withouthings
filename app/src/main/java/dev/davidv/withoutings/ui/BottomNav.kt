package dev.davidv.withoutings.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.FitnessCenter
import androidx.compose.material.icons.rounded.Home
import androidx.compose.material.icons.rounded.ViewDay
import androidx.compose.material.icons.rounded.Watch
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.unit.dp
import dev.davidv.withoutings.ui.theme.AppTheme

/**
 * The four places the app is, as opposed to the places it goes.
 *
 * App-level settings are deliberately not here — the gear on Now opens those.
 * "Watch" is strictly about the physical device, which is what keeps the two
 * kinds of setting from being looked for in the same place.
 */
enum class Tab(val route: String, val label: String, val icon: ImageVector) {
    Now("now", "Now", Icons.Rounded.Home),
    Today("today", "Today", Icons.Rounded.ViewDay),
    Activity("activities", "Activity", Icons.Rounded.FitnessCenter),
    Watch("watch", "Watch", Icons.Rounded.Watch),
}

@Composable
fun BottomNav(current: Tab, onSelect: (Tab) -> Unit) {
    Row(
        Modifier
            .fillMaxWidth()
            .background(MaterialTheme.colorScheme.surfaceContainer)
            .navigationBarsPadding()
            .height(AppTheme.space.bottomNavHeight - AppTheme.space.gestureBar),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Tab.entries.forEach { tab ->
            NavItem(tab, tab == current, Modifier.weight(1f)) { onSelect(tab) }
        }
    }
}

@Composable
private fun NavItem(tab: Tab, selected: Boolean, modifier: Modifier, onClick: () -> Unit) {
    Column(
        modifier.clickable(onClick = onClick),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(3.dp, Alignment.CenterVertically),
    ) {
        Box(
            Modifier
                .clip(AppTheme.pill)
                .background(
                    if (selected) {
                        MaterialTheme.colorScheme.primaryContainer
                    } else {
                        androidx.compose.ui.graphics.Color.Transparent
                    }
                )
                .padding(horizontal = 18.dp, vertical = 4.dp),
        ) {
            Icon(
                tab.icon,
                null,
                Modifier.size(21.dp),
                tint = if (selected) {
                    MaterialTheme.colorScheme.onPrimaryContainer
                } else {
                    MaterialTheme.colorScheme.onSurfaceVariant
                },
            )
        }
        Text(
            tab.label,
            style = if (selected) AppTheme.type.navLabelActive else AppTheme.type.navLabel,
            color = if (selected) {
                MaterialTheme.colorScheme.onSurface
            } else {
                MaterialTheme.colorScheme.onSurfaceVariant
            },
        )
    }
}
