package dev.davidv.withoutings.ui.theme

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * Every value the design is made of, in one place.
 *
 * Screens never write a hex, a dp or an sp of their own: they read a role from
 * [MaterialTheme][androidx.compose.material3.MaterialTheme] where one exists,
 * and from [AppTheme] where the design asks for something Material 3 has no
 * role for. Swapping the palette is then editing [LightPalette] and nothing
 * else.
 */
data class Palette(
    /// The Material 3 roles the design's colours stand for. Named after the
    /// role rather than the colour so a repaint does not make the name a lie.
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

    /// Secondary text inside an accent card, where onPrimaryContainer is too
    /// heavy for a sentence.
    val onAccentSecondary: Color,
    /// Meta text: units, contexts, timestamps beside a value.
    val onSurfaceTertiary: Color,
    /// Axis labels and anything deliberately receding.
    val onSurfaceDim: Color,
    /// A disabled control, which must read as unavailable rather than quiet.
    val onSurfaceDisabled: Color,
    /// The stroke a data series is drawn with — darker than [primary], which
    /// is a UI colour and washes out against a filled area.
    val dataStroke: Color,
    /// Ruling inside a chart card, lighter than a divider between rows.
    val chartGrid: Color,
    /// A list row singled out, without turning it into an accent card.
    val rowHighlight: Color,
    /// The unfilled half of a progress track or a ring.
    val track: Color,
    val ringTrack: Color,
    /// A bar that came out below par. Deliberately neutral: this app never
    /// implies an alarm.
    val barBelow: Color,
    /// The knob of an off toggle, and the ring that keeps it visible against
    /// its own track.
    val toggleOffKnob: Color,
    val toggleOffKnobRing: Color,
    /// Text on the dark "now" card, quieter than white.
    val onInverseSecondary: Color,
    /// A drag handle at rest, and while it is being held.
    val dragHandle: Color,
    val dragHandleActive: Color,

    val sleepDeep: Color,
    val sleepRem: Color,
    val sleepLight: Color,
    val sleepAwake: Color,

    /// The one warm surface in the app, following clinical ECG paper.
    val ecgPaper: Color,
    val ecgPaperBorder: Color,
    val ecgGrid: Color,
    val ecgTrace: Color,
    val ecgMeta: Color,
)

/**
 * Seeded on the teal in [Palette.primary]. Light only: the design rejects a
 * dark scheme outright, including for the live workout screen, where an
 * inverted version was tried and thrown out.
 */
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

/**
 * How far apart things sit.
 *
 * The design gives a range for most gaps rather than one number, because a
 * denser screen closes them up; the names say which screen asked for which.
 */
data class Spacing(
    /// Every screen's left and right margin. Nothing is inset differently.
    val screen: Dp = 18.dp,
    /// Between content blocks, tightest on Now and loosest on Sleep and ECG.
    val blockTight: Dp = 9.dp,
    val block: Dp = 10.dp,
    val blockMetric: Dp = 11.dp,
    val blockLoose: Dp = 13.dp,
    /// Inside a tile.
    val tile: Dp = 13.dp,
    val tileHorizontal: Dp = 14.dp,
    /// Above and below a list row's content.
    val listRow: Dp = 7.dp,
    /// Where a divider starts, so it clears the leading icon and its gap.
    val dividerInset: Dp = 55.dp,
    /// Between a leading icon and the text beside it.
    val leading: Dp = 13.dp,
    val bottomNavHeight: Dp = 80.dp,
    /// Room under the bottom nav for the system gesture bar.
    val gestureBar: Dp = 12.dp,
    /// The left rail on Today, and the height its hour scale is drawn in.
    val railWidth: Dp = 52.dp,
)

/** Corner radii, largest to smallest. */
data class Radii(
    /// Hero and accent cards; the screen's own corners in the mock.
    val hero: Dp = 26.dp,
    /// The accent summary card on a history screen.
    val accent: Dp = 22.dp,
    /// Home tiles.
    val tile: Dp = 20.dp,
    /// Secondary tiles, chart cards, event cards.
    val card: Dp = 18.dp,
    /// Filled buttons and list rows.
    val button: Dp = 16.dp,
    /// Small chart cards and activity icons.
    val small: Dp = 14.dp,
    /// A checkbox in the reorderable list.
    val checkbox: Dp = 8.dp,
    /// Filter chips.
    val chip: Dp = 9.dp,
    /// Pills: battery, nav indicator, outline buttons, toggles, tracks.
    val pill: Dp = 99.dp,
    /// Zone-bar segments.
    val segment: Dp = 3.dp,
)

/**
 * Type sizes and weights.
 *
 * Machine facts are monospace and human sentences are not, which is why sizes
 * come in pairs here rather than one scale: the mono face sits differently
 * against the same nominal size.
 */
data class TypeScale(
    val titleHome: TextUnit = 30.sp,
    val titleDetail: TextUnit = 22.sp,
    /// The one number a screen is about, at the two sizes the design uses.
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
    /// Section headings that are words rather than eyebrows.
    val section: TextUnit = 13.sp,
    val sectionLarge: TextUnit = 13.5.sp,
    val cardTitle: TextUnit = 15.sp,
    val resultTitle: TextUnit = 17.sp,
    /// Letter-spacing an eyebrow is tracked out by.
    val eyebrowTracking: TextUnit = 0.66.sp,
)

/** Line weights and fill opacities inside a chart. */
data class ChartTokens(
    val trace: Dp = 2.dp,
    val traceHeavy: Dp = 2.5.dp,
    val traceThin: Dp = 1.6.dp,
    val grid: Dp = 0.8.dp,
    val cursor: Dp = 1.dp,
    val cursorDot: Dp = 4.dp,
    /// Under the trace.
    val areaAlpha: Float = 0.12f,
    val areaAlphaLight: Float = 0.10f,
    /// The window a recorded session covers, shaded behind everything.
    val sessionAlpha: Float = 0.07f,
    /// A horizontal band saying where this person's readings usually sit.
    val bandAlpha: Float = 0.12f,
    /// Heart-rate zones, from the top band down.
    val zoneHighAlpha: Float = 0.16f,
    val zoneAlpha: Float = 0.09f,
    /// The legend swatches that name the two shadings above.
    val legendBandAlpha: Float = 0.18f,
    val legendSessionAlpha: Float = 0.09f,
    /// Minor ruling on ECG paper, against its major ruling.
    val ecgMinorAlpha: Float = 0.55f,
)

val DefaultSpacing = Spacing()
val DefaultRadii = Radii()
val DefaultTypeScale = TypeScale()
val DefaultChartTokens = ChartTokens()
