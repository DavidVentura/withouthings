package dev.davidv.withoutings.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.unit.dp
import dev.davidv.withoutings.ui.theme.AppTheme
import uniffi.wpp_ffi.EcgRhythm
import uniffi.wpp_ffi.EcgRecording

/// The rate the watch streams and records a waveform at.
const val LIVE_ECG_HZ = 300

/// Six seconds is what a clinical strip shows on one line at 25 mm/s.
private const val STRIP_SECONDS = 6

/**
 * A recording, on paper.
 *
 * The warm surface here is the only one in the app, and it is warm because
 * that is what an ECG has always been printed on — the pink graph paper is
 * what makes a rhythm strip readable rather than decorative.
 */
@Composable
fun EcgDetailScreen(
    recording: EcgRecording?,
    window: LongRange,
    onWindowChange: (LongRange) -> Unit,
    onBack: () -> Unit,
) {

    val shown = recording?.leads
        ?.filter { it.name.endsWith("FILTERED") }
        ?.ifEmpty { recording.leads }
        .orEmpty()
    val step = recording?.let { 1000.0 / it.samplingHz.toDouble() } ?: 1.0
    val traces = shown.map { lead ->
        // Electrode DC would push the trace off a fixed axis. Only the offset
        // goes; the millivolt scale is untouched, so the squares still measure
        // what they claim to.
        val baseline = if (lead.millivolts.isEmpty()) 0.0 else {
            lead.millivolts.sorted()[lead.millivolts.size / 2]
        }
        lead.name to lead.millivolts.mapIndexed { index, mv ->
            ChartPoint((recording?.measuredAtMs ?: 0) + (index * step).toLong(), mv - baseline)
        }
    }

    DetailScaffold(
        title = "Electrocardiogram",
        subtitle = recording?.let {
            "${fullDate(it.measuredAtMs)} · ${clockWithSeconds(it.measuredAtMs)}"
        },
        onBack = onBack,
        gap = AppTheme.space.blockLoose,
    ) {
        if (recording == null) {
            EmptyNote("That recording is not here.")
            return@DetailScaffold
        }

        Column(
            Modifier.weight(1f).verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(AppTheme.space.blockLoose),
        ) {
            ResultCard(recording.rhythm)

            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                Text(
                    "25 mm/s · 10 mm/mV · ${recording.samplingHz} Hz",
                    style = AppTheme.type.axis,
                    color = AppTheme.colors.onSurfaceTertiary,
                )
            }

            traces.forEach { (_, points) ->
                PaperCard {
                    ValueChart(
                        points = points,
                        window = window,
                        axis = -1.0..1.0,
                        decimals = 1,
                        height = 200.dp,
                        onWindowChange = onWindowChange,
                        grid = GridStyle.EcgPaper,
                        limit = recording.measuredAtMs..(
                            recording.measuredAtMs + (points.size * step).toLong()
                            ),
                        lineColor = AppTheme.colors.ecgTrace,
                        fillColor = AppTheme.colors.ecgTrace,
                        unit = " mV",
                    )
                }
            }

            Spacer(Modifier.height(8.dp))
        }
    }
}

/**
 * The rhythm, as the watch read it.
 *
 * The classifier runs on the watch and its answer arrives with the waveform, so
 * this reports a result rather than making one. Nothing here reads a rhythm off
 * the trace: that would be the medical claim the design forbids, and it stays
 * forbidden. A recording that arrived without a verdict — every one synced
 * before this app began collecting them — says so plainly.
 */
@Composable
private fun ResultCard(rhythm: EcgRhythm?) {
    AccentCard(Modifier.fillMaxWidth()) {
        Text(
            when (rhythm) {
                EcgRhythm.NO_AFIB -> "No signs of atrial fibrillation"
                EcgRhythm.AFIB -> "Signs of atrial fibrillation"
                EcgRhythm.INCONCLUSIVE -> "Inconclusive"
                EcgRhythm.POOR_RECORDING -> "Poor recording"
                EcgRhythm.RATE_OUT_OF_RANGE -> "Heart rate outside the range it judges"
                EcgRhythm.NO_RESULT -> "The watch reached no result"
                null -> "No interpretation"
            },
            style = AppTheme.type.resultTitle,
            color = MaterialTheme.colorScheme.onPrimaryContainer,
        )
    }
}

/** The warm paper card the strips are printed on. */
@Composable
private fun PaperCard(content: @Composable () -> Unit) {
    val shape = RoundedCornerShape(AppTheme.radius.small)
    Column(
        Modifier
            .fillMaxWidth()
            .clip(shape)
            .background(AppTheme.colors.ecgPaper)
            .border(1.dp, AppTheme.colors.ecgPaperBorder, shape)
            .padding(8.dp),
    ) {
        content()
    }
}

/**
 * A recording as it is taken.
 *
 * These samples exist nowhere else until the recording finishes and transfers,
 * which is why the screen has to stay up: the watch stops sending the waveform
 * when nothing is showing it.
 */
@Composable
fun LiveEcgScreen(
    millivolts: List<Double>,
    samplingHz: Int,
    recording: Boolean,
    window: LongRange?,
    onWindowChange: (LongRange) -> Unit,
    onBack: () -> Unit,
) {
    val step = 1000.0 / samplingHz
    val seconds = millivolts.size.toDouble() / samplingHz
    val span = (STRIP_SECONDS * 1000).toLong()
    val end = (millivolts.size * step).toLong()
    // The window slides with the newest sample rather than compressing to fit,
    // so it reads like paper coming out of a machine.
    val follow = (end - span).coerceAtLeast(0L)
    val shown = window ?: if (recording) follow..(follow + span) else 0L..span
    val baseline = if (millivolts.isEmpty()) 0.0 else {
        millivolts.takeLast(samplingHz * 2).sorted().let { it[it.size / 2] }
    }

    DetailScaffold(
        title = if (recording) "Recording" else "Recording complete",
        subtitle = "${grouped(seconds, 1)} s · $samplingHz Hz · 25 mm/s · 10 mm/mV",
        onBack = onBack,
        gap = AppTheme.space.blockLoose,
    ) {
        PaperCard {
            ValueChart(
                points = millivolts.mapIndexed { index, mv ->
                    ChartPoint((index * step).toLong(), mv - baseline)
                },
                window = shown,
                axis = -1.0..1.0,
                decimals = 1,
                height = 220.dp,
                onWindowChange = onWindowChange,
                grid = GridStyle.EcgPaper,
                limit = 0L..end.coerceAtLeast(span),
                lineColor = AppTheme.colors.ecgTrace,
                fillColor = AppTheme.colors.ecgTrace,
                unit = " mV",
            )
        }
        if (recording) {
            EmptyNote(
                "Keep the app open: the watch stops sending the waveform when " +
                    "nothing is showing it."
            )
        }
    }
}
