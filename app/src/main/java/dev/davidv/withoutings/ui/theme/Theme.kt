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

/**
 * The palette expressed as Material 3 roles.
 *
 * The design was chosen to be expressible as a tonal scheme, so the roles carry
 * it and screens can go on using [MaterialTheme.colorScheme]. What has no role
 * — data strokes, sleep stages, ECG paper — lives on [AppTheme] instead of
 * being forced into `tertiary` and friends, where the name would say nothing
 * about what the colour is for.
 */
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
    // The app has no error state to draw. Destructive actions are worded
    // rather than coloured, and the design forbids reds outright — but a
    // stock dialog will still reach for these, so they resolve to something
    // that belongs on screen.
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

/** Everything the design asks for that Material 3 has no role for. */
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

    /** Shorthand for the shapes named often enough to be worth one. */
    val pill: Shape
        @Composable @ReadOnlyComposable
        get() = androidx.compose.foundation.shape.RoundedCornerShape(LocalRadii.current.pill)

    val square: Shape get() = RectangleShape
}

/**
 * Light only, and not tinted by the system wallpaper.
 *
 * Material You would repaint a palette that the design leans on for meaning —
 * the green tonal set is what lets an elevated heart rate be shown without
 * implying an alarm, and a wallpaper-derived orange would undo exactly that.
 */
@Composable
fun WithoutingsTheme(
    palette: Palette = LightPalette,
    spacing: Spacing = DefaultSpacing,
    radii: Radii = DefaultRadii,
    scale: TypeScale = DefaultTypeScale,
    chart: ChartTokens = DefaultChartTokens,
    content: @Composable () -> Unit,
) {
    // The window draws behind the status bar; without this the system clock and
    // icons stay light-on-light over the app's background.
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
