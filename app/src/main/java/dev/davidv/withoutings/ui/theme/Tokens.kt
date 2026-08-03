package dev.davidv.withoutings.ui.theme

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

data class Palette(
    val surface: Color,
    val surfaceContainer: Color,
    val surfaceContainerHigh: Color,
    val surfaceContainerLowest: Color,
    val surfaceVariant: Color,
    val primary: Color,
    val onPrimary: Color,
    val primaryContainer: Color,
    val onPrimaryContainer: Color,
    val onSurface: Color,
    val onSurfaceVariant: Color,
    val outline: Color,
    val outlineVariant: Color,

    val onAccentSecondary: Color,
    val onSurfaceTertiary: Color,
    val onSurfaceDim: Color,
    val onSurfaceDisabled: Color,
    val dataStroke: Color,
    val chartGrid: Color,
    val rowHighlight: Color,
    val track: Color,
    val ringTrack: Color,
    val barBelow: Color,
    val toggleOffKnob: Color,
    val toggleOffKnobRing: Color,
    val onInverseSecondary: Color,
    val dragHandle: Color,
    val dragHandleActive: Color,

    val sleepDeep: Color,
    val sleepRem: Color,
    val sleepLight: Color,
    val sleepAwake: Color,

    val ecgPaper: Color,
    val ecgPaperBorder: Color,
    val ecgGrid: Color,
    val ecgTrace: Color,
    val ecgMeta: Color,
)

val LightPalette = Palette(
    surface = Color(0xFFF5F9F7),
    surfaceContainer = Color(0xFFE9EFEC),
    surfaceContainerHigh = Color(0xFFE3EAE7),
    surfaceContainerLowest = Color(0xFFFFFFFF),
    surfaceVariant = Color(0xFFCBD6D1),
    primary = Color(0xFF16695B),
    onPrimary = Color(0xFFFFFFFF),
    primaryContainer = Color(0xFFA2F2DE),
    onPrimaryContainer = Color(0xFF00201A),
    onSurface = Color(0xFF171D1B),
    onSurfaceVariant = Color(0xFF3F4946),
    outline = Color(0xFFBFC9C3),
    outlineVariant = Color(0xFFDCE4E0),

    onAccentSecondary = Color(0xFF2C4E46),
    onSurfaceTertiary = Color(0xFF6F7976),
    onSurfaceDim = Color(0xFF8A9490),
    onSurfaceDisabled = Color(0xFFC0C9C5),
    dataStroke = Color(0xFF0C4C41),
    chartGrid = Color(0xFFEDF2F0),
    rowHighlight = Color(0xFFEFF4F1),
    track = Color(0xFFCBD6D1),
    ringTrack = Color(0xFF7FD5BE),
    barBelow = Color(0xFF8A9490),
    toggleOffKnob = Color(0xFFF5F9F7),
    toggleOffKnobRing = Color(0xFFA9B4B0),
    onInverseSecondary = Color(0xFFCFEFE5),
    dragHandle = Color(0xFFB4BFBB),
    dragHandleActive = Color(0xFF6F7976),

    sleepDeep = Color(0xFF16695B),
    sleepRem = Color(0xFF5E9C8C),
    sleepLight = Color(0xFFA2CFC3),
    sleepAwake = Color(0xFFC9D3CF),

    ecgPaper = Color(0xFFFFF9F8),
    ecgPaperBorder = Color(0xFFEBD9D6),
    ecgGrid = Color(0xFFF0C9C2),
    ecgTrace = Color(0xFFB4522F),
    ecgMeta = Color(0xFFA88A84),
)

data class Spacing(
    val screen: Dp = 18.dp,
    val blockTight: Dp = 9.dp,
    val block: Dp = 10.dp,
    val blockMetric: Dp = 11.dp,
    val blockLoose: Dp = 13.dp,
    val tile: Dp = 13.dp,
    val tileHorizontal: Dp = 14.dp,
    val listRow: Dp = 7.dp,
    val dividerInset: Dp = 55.dp,
    val leading: Dp = 13.dp,
    val bottomNavHeight: Dp = 80.dp,
    val gestureBar: Dp = 12.dp,
    val railWidth: Dp = 52.dp,
)

data class Radii(
    val hero: Dp = 26.dp,
    val accent: Dp = 22.dp,
    val tile: Dp = 20.dp,
    val card: Dp = 18.dp,
    val button: Dp = 16.dp,
    val small: Dp = 14.dp,
    val checkbox: Dp = 8.dp,
    val chip: Dp = 9.dp,
    val pill: Dp = 99.dp,
    val segment: Dp = 3.dp,
)

data class TypeScale(
    val titleHome: TextUnit = 30.sp,
    val titleDetail: TextUnit = 22.sp,
    val focalLarge: TextUnit = 92.sp,
    val focalMedium: TextUnit = 44.sp,
    val focalSmall: TextUnit = 42.sp,
    val tileValue: TextUnit = 31.sp,
    val statValue: TextUnit = 26.sp,
    val summaryValue: TextUnit = 28.sp,
    val eyebrow: TextUnit = 11.sp,
    val eyebrowLarge: TextUnit = 12.sp,
    val rowTitle: TextUnit = 14.5.sp,
    val rowMeta: TextUnit = 11.5.sp,
    val tileLabel: TextUnit = 12.sp,
    val tileContext: TextUnit = 10.5.sp,
    val body: TextUnit = 12.sp,
    val bodyLarge: TextUnit = 12.5.sp,
    val navLabel: TextUnit = 11.sp,
    val axis: TextUnit = 10.sp,
    val axisLarge: TextUnit = 10.5.sp,
    val section: TextUnit = 13.sp,
    val sectionLarge: TextUnit = 13.5.sp,
    val cardTitle: TextUnit = 15.sp,
    val resultTitle: TextUnit = 17.sp,
    val eyebrowTracking: TextUnit = 0.66.sp,
)

data class ChartTokens(
    val trace: Dp = 2.dp,
    val traceHeavy: Dp = 2.5.dp,
    val traceThin: Dp = 1.6.dp,
    val grid: Dp = 0.8.dp,
    val cursor: Dp = 1.dp,
    val cursorDot: Dp = 4.dp,
    val areaAlpha: Float = 0.12f,
    val areaAlphaLight: Float = 0.10f,
    val sessionAlpha: Float = 0.07f,
    val zoneHighAlpha: Float = 0.16f,
    val zoneAlpha: Float = 0.09f,
    val legendSessionAlpha: Float = 0.09f,
    val ecgMinorAlpha: Float = 0.55f,
)

val DefaultSpacing = Spacing()
val DefaultRadii = Radii()
val DefaultTypeScale = TypeScale()
val DefaultChartTokens = ChartTokens()
