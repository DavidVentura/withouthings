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

const val LIVE_ECG_HZ = 300

private const val STRIP_SECONDS = 6

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
