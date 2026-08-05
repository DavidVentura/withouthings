package dev.davidv.withoutings.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import dev.davidv.withoutings.ui.theme.AppTheme

@Composable
fun StorageScreen(onGrant: () -> Unit) {
    Column(
        Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.surface)
            .padding(AppTheme.space.screen),
        verticalArrangement = Arrangement.spacedBy(
            AppTheme.space.blockMetric,
            Alignment.CenterVertically,
        ),
    ) {
        Text(
            "Grant access so health data can be stored outside the app's internal " +
                "storage. This means that the data will survive the app being " +
                "uninstalled/reinstalled and that you can back it up off device, " +
                "if you wish.",
            style = AppTheme.type.resultTitle,
            color = AppTheme.colors.onSurfaceTertiary,
        )
        OutlineAction("Grant file access", Modifier.fillMaxWidth(), onClick = onGrant)
    }
}
