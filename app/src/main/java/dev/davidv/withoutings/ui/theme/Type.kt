package dev.davidv.withoutings.ui.theme

import androidx.compose.material3.Typography
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.LineHeightStyle
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.em
import androidx.compose.ui.unit.sp

/**
 * Roboto for anything a person wrote, monospace for anything the watch
 * produced — timestamps, counts, sample rates, axis labels, section eyebrows.
 * The split is the design's main typographic idea, so it is spelled out in the
 * style names rather than left to each callsite.
 */
private val Human = FontFamily.Default
private val Machine = FontFamily.Monospace

/// Large numbers are set tight; at 92sp the default leading leaves a hole
/// above the digits that pushes everything beside them out of alignment.
private fun focal(size: TextUnit, weight: FontWeight, tracking: TextUnit) = TextStyle(
    fontFamily = Human,
    fontWeight = weight,
    fontSize = size,
    lineHeight = size * 0.92f,
    letterSpacing = tracking,
    lineHeightStyle = LineHeightStyle(
        alignment = LineHeightStyle.Alignment.Center,
        trim = LineHeightStyle.Trim.Both,
    ),
)

/**
 * The styles the design names, over and above Material's own scale.
 *
 * Held as data rather than as top-level vals so a future theme can restate
 * them; every screen reads them through [AppTheme].
 */
data class AppTypography(
    val titleHome: TextStyle,
    val titleDetail: TextStyle,
    /// The subtitle under a title: which day, which window.
    val titleMeta: TextStyle,
    val focalLarge: TextStyle,
    val focalMedium: TextStyle,
    val focalInverse: TextStyle,
    val tileValue: TextStyle,
    val statValue: TextStyle,
    val summaryValue: TextStyle,
    /// A mono, tracked-out, upper-case section marker.
    val eyebrow: TextStyle,
    val eyebrowLarge: TextStyle,
    val sectionTitle: TextStyle,
    val cardTitle: TextStyle,
    val resultTitle: TextStyle,
    val rowTitle: TextStyle,
    val rowMeta: TextStyle,
    val tileLabel: TextStyle,
    val tileContext: TextStyle,
    val unit: TextStyle,
    /// Observation copy: the only place the app writes sentences.
    val body: TextStyle,
    val bodyLarge: TextStyle,
    val navLabelActive: TextStyle,
    val navLabel: TextStyle,
    val axis: TextStyle,
    val axisSmall: TextStyle,
    /// Inside a scrub tooltip, which is small, dark and read at a glance.
    val tooltip: TextStyle,
    val buttonLabel: TextStyle,
    val chipLabel: TextStyle,
)

fun appTypography(scale: TypeScale = DefaultTypeScale) = AppTypography(
    titleHome = TextStyle(
        fontFamily = Human,
        fontWeight = FontWeight.Normal,
        fontSize = scale.titleHome,
        lineHeight = scale.titleHome * 1.05f,
        letterSpacing = (-0.6).sp,
    ),
    titleDetail = TextStyle(
        fontFamily = Human,
        fontWeight = FontWeight.Normal,
        fontSize = scale.titleDetail,
        lineHeight = scale.titleDetail * 1.1f,
    ),
    titleMeta = TextStyle(
        fontFamily = Machine,
        fontWeight = FontWeight.Normal,
        fontSize = scale.rowMeta,
    ),
    focalLarge = focal(scale.focalLarge, FontWeight.Light, (-5).sp),
    focalMedium = focal(scale.focalMedium, FontWeight.Light, (-2).sp),
    focalInverse = focal(scale.focalSmall, FontWeight.Light, (-2).sp),
    tileValue = focal(scale.tileValue, FontWeight.Normal, (-1.2).sp),
    statValue = focal(scale.statValue, FontWeight.Normal, (-1).sp),
    summaryValue = focal(scale.summaryValue, FontWeight.Normal, (-1).sp),
    eyebrow = TextStyle(
        fontFamily = Machine,
        fontWeight = FontWeight.Medium,
        fontSize = scale.eyebrow,
        letterSpacing = 0.06.em,
    ),
    eyebrowLarge = TextStyle(
        fontFamily = Machine,
        fontWeight = FontWeight.Medium,
        fontSize = scale.eyebrowLarge,
        letterSpacing = 0.06.em,
    ),
    sectionTitle = TextStyle(
        fontFamily = Human,
        fontWeight = FontWeight.Medium,
        fontSize = scale.section,
    ),
    cardTitle = TextStyle(
        fontFamily = Human,
        fontWeight = FontWeight.Medium,
        fontSize = scale.cardTitle,
    ),
    resultTitle = TextStyle(
        fontFamily = Human,
        fontWeight = FontWeight.Medium,
        fontSize = scale.resultTitle,
    ),
    rowTitle = TextStyle(
        fontFamily = Human,
        fontWeight = FontWeight.Medium,
        fontSize = scale.rowTitle,
    ),
    rowMeta = TextStyle(
        fontFamily = Machine,
        fontWeight = FontWeight.Normal,
        fontSize = scale.rowMeta,
    ),
    tileLabel = TextStyle(
        fontFamily = Human,
        fontWeight = FontWeight.Medium,
        fontSize = scale.tileLabel,
    ),
    tileContext = TextStyle(
        fontFamily = Machine,
        fontWeight = FontWeight.Normal,
        fontSize = scale.tileContext,
    ),
    unit = TextStyle(
        fontFamily = Human,
        fontWeight = FontWeight.Normal,
        fontSize = scale.tileLabel,
    ),
    body = TextStyle(
        fontFamily = Human,
        fontWeight = FontWeight.Normal,
        fontSize = scale.body,
        lineHeight = scale.body * 1.4f,
    ),
    bodyLarge = TextStyle(
        fontFamily = Human,
        fontWeight = FontWeight.Normal,
        fontSize = scale.bodyLarge,
        lineHeight = scale.bodyLarge * 1.4f,
    ),
    navLabelActive = TextStyle(
        fontFamily = Human,
        fontWeight = FontWeight.Medium,
        fontSize = scale.navLabel,
    ),
    navLabel = TextStyle(
        fontFamily = Human,
        fontWeight = FontWeight.Normal,
        fontSize = scale.navLabel,
    ),
    axis = TextStyle(
        fontFamily = Machine,
        fontWeight = FontWeight.Normal,
        fontSize = scale.axisLarge,
    ),
    axisSmall = TextStyle(
        fontFamily = Machine,
        fontWeight = FontWeight.Normal,
        fontSize = scale.axis,
    ),
    tooltip = TextStyle(
        fontFamily = Machine,
        fontWeight = FontWeight.Medium,
        fontSize = scale.eyebrow,
    ),
    buttonLabel = TextStyle(
        fontFamily = Human,
        fontWeight = FontWeight.Medium,
        fontSize = scale.rowTitle,
    ),
    chipLabel = TextStyle(
        fontFamily = Human,
        fontWeight = FontWeight.Medium,
        fontSize = scale.bodyLarge,
    ),
)

/**
 * Material's own scale, for the components that read it directly — dialogs,
 * switches, the pull-to-refresh label. Kept in step with [appTypography] so a
 * stock component does not arrive in a different voice.
 */
fun materialTypography(scale: TypeScale = DefaultTypeScale): Typography {
    val app = appTypography(scale)
    return Typography(
        displayLarge = app.focalLarge,
        displayMedium = app.focalMedium,
        displaySmall = app.summaryValue,
        headlineLarge = app.titleHome,
        headlineMedium = app.titleDetail,
        headlineSmall = app.titleDetail,
        titleLarge = app.titleDetail,
        titleMedium = app.cardTitle,
        titleSmall = app.rowTitle,
        bodyLarge = app.bodyLarge,
        bodyMedium = app.body,
        bodySmall = app.body,
        labelLarge = app.buttonLabel,
        labelMedium = app.tileLabel,
        labelSmall = app.eyebrow,
    )
}
