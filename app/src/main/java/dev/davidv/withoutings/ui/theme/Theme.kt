package dev.davidv.withoutings.ui.theme

import android.app.Activity
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.ReadOnlyComposable
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.graphics.RectangleShape
import androidx.compose.ui.platform.LocalView
import androidx.core.view.WindowCompat

private fun schemeOf(palette: Palette) = lightColorScheme(
    primary = palette.primary,
    onPrimary = palette.onPrimary,
    primaryContainer = palette.primaryContainer,
    onPrimaryContainer = palette.onPrimaryContainer,
    secondary = palette.primary,
    onSecondary = palette.onPrimary,
    secondaryContainer = palette.surfaceContainerHigh,
    onSecondaryContainer = palette.onSurfaceVariant,
    tertiary = palette.sleepRem,
    onTertiary = palette.onPrimary,
    tertiaryContainer = palette.sleepLight,
    onTertiaryContainer = palette.onPrimaryContainer,
    background = palette.surface,
    onBackground = palette.onSurface,
    surface = palette.surface,
    onSurface = palette.onSurface,
    surfaceVariant = palette.surfaceVariant,
    onSurfaceVariant = palette.onSurfaceVariant,
    surfaceContainerLowest = palette.surfaceContainerLowest,
    surfaceContainerLow = palette.surface,
    surfaceContainer = palette.surfaceContainer,
    surfaceContainerHigh = palette.surfaceContainerHigh,
    surfaceContainerHighest = palette.surfaceContainerHigh,
    outline = palette.outline,
    outlineVariant = palette.outlineVariant,
    inverseSurface = palette.onSurface,
    inverseOnSurface = palette.surface,
    error = palette.onSurfaceVariant,
    onError = palette.onPrimary,
    errorContainer = palette.surfaceContainerHigh,
    onErrorContainer = palette.onSurface,
)

private fun shapesOf(radii: Radii) = Shapes(
    extraSmall = androidx.compose.foundation.shape.RoundedCornerShape(radii.checkbox),
    small = androidx.compose.foundation.shape.RoundedCornerShape(radii.chip),
    medium = androidx.compose.foundation.shape.RoundedCornerShape(radii.small),
    large = androidx.compose.foundation.shape.RoundedCornerShape(radii.card),
    extraLarge = androidx.compose.foundation.shape.RoundedCornerShape(radii.hero),
)

private val LocalPalette = staticCompositionLocalOf { LightPalette }
private val LocalSpacing = staticCompositionLocalOf { DefaultSpacing }
private val LocalRadii = staticCompositionLocalOf { DefaultRadii }
private val LocalAppTypography = staticCompositionLocalOf { appTypography() }
private val LocalChartTokens = staticCompositionLocalOf { DefaultChartTokens }

object AppTheme {
    val colors: Palette
        @Composable @ReadOnlyComposable get() = LocalPalette.current

    val space: Spacing
        @Composable @ReadOnlyComposable get() = LocalSpacing.current

    val radius: Radii
        @Composable @ReadOnlyComposable get() = LocalRadii.current

    val type: AppTypography
        @Composable @ReadOnlyComposable get() = LocalAppTypography.current

    val chart: ChartTokens
        @Composable @ReadOnlyComposable get() = LocalChartTokens.current

    val pill: Shape
        @Composable @ReadOnlyComposable
        get() = androidx.compose.foundation.shape.RoundedCornerShape(LocalRadii.current.pill)

    val square: Shape get() = RectangleShape
}

@Composable
fun WithoutingsTheme(
    palette: Palette = LightPalette,
    spacing: Spacing = DefaultSpacing,
    radii: Radii = DefaultRadii,
    scale: TypeScale = DefaultTypeScale,
    chart: ChartTokens = DefaultChartTokens,
    content: @Composable () -> Unit,
) {
    val view = LocalView.current
    if (!view.isInEditMode) {
        SideEffect {
            val window = (view.context as Activity).window
            WindowCompat.getInsetsController(window, view).apply {
                isAppearanceLightStatusBars = true
                isAppearanceLightNavigationBars = true
            }
        }
    }

    CompositionLocalProvider(
        LocalPalette provides palette,
        LocalSpacing provides spacing,
        LocalRadii provides radii,
        LocalAppTypography provides appTypography(scale),
        LocalChartTokens provides chart,
    ) {
        MaterialTheme(
            colorScheme = schemeOf(palette),
            typography = materialTypography(scale),
            shapes = shapesOf(radii),
            content = content,
        )
    }
}
