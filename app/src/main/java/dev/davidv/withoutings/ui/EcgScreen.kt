package dev.davidv.withoutings.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import uniffi.wpp_ffi.EcgRecording
import uniffi.wpp_ffi.EcgSummary

private val ecgStamp = SimpleDateFormat("d MMM HH:mm:ss", Locale.getDefault())

@Composable
fun EcgListScreen(recordings: List<EcgSummary>, onSelect: (EcgSummary) -> Unit) {
    Column(
        Modifier.fillMaxSize().statusBarsPadding().padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Text("ECG", style = MaterialTheme.typography.headlineMedium)
        if (recordings.isEmpty()) {
            Text(
                "No recordings. Start one from the watch; it transfers on the " +
                    "next sync, and the watch drops its copy once it has.",
                style = MaterialTheme.typography.bodySmall,
            )
        }
        LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            items(recordings) { recording ->
                Card(Modifier.fillMaxWidth().clickable { onSelect(recording) }) {
                    Column(Modifier.padding(12.dp)) {
                        Text(
                            ecgStamp.format(Date(recording.measuredAtMs)),
                            style = MaterialTheme.typography.titleMedium,
                        )
                        Text(
                            listOfNotNull(
                                "${recording.seconds.toInt()} seconds",
                                recording.heartRate?.let { "$it bpm" },
                            ).joinToString(" · "),
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                }
            }
        }
    }
}

/**
 * One recording, a lead per chart on a shared time window.
 *
 * The leads are simultaneous, so panning one has to move them all; they are the
 * same instant viewed through different electrodes.
 */
@Composable
fun EcgDetailScreen(
    recording: EcgRecording?,
    window: LongRange,
    onWindowChange: (LongRange) -> Unit,
) {
    Column(
        Modifier.fillMaxSize().statusBarsPadding().padding(16.dp)
            .verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        if (recording == null) {
            Text("Recording not found", style = MaterialTheme.typography.headlineMedium)
            return@Column
        }
        Text(
            ecgStamp.format(Date(recording.measuredAtMs)),
            style = MaterialTheme.typography.headlineMedium,
        )
        Text(
            "${recording.samplingHz} Hz · 25 mm/s · 10 mm/mV",
            style = MaterialTheme.typography.bodySmall,
        )

        val step = 1000.0 / recording.samplingHz.toDouble()
        val extent = recording.measuredAtMs..(
            recording.measuredAtMs +
                ((recording.leads.firstOrNull()?.millivolts?.size ?: 0) * step).toLong()
            )

        // The watch's own filter output is what is worth reading; the raw lead
        // carries baseline wander and mains hum that obscure it.
        val shown = recording.leads.filter { it.name.endsWith("FILTERED") }
            .ifEmpty { recording.leads }
        shown.forEach { lead ->
            // An unfiltered lead carries several millivolts of electrode DC,
            // which would push the trace off a fixed axis. Only the offset is
            // removed; the millivolt scale is untouched, so the squares still
            // measure what they claim to.
            val baseline = if (lead.millivolts.isEmpty()) 0.0 else {
                lead.millivolts.sorted()[lead.millivolts.size / 2]
            }
            ValueChart(
                points = lead.millivolts.mapIndexed { index, mv ->
                    ChartPoint(recording.measuredAtMs + (index * step).toLong(), mv - baseline)
                },
                markers = emptyList(),
                window = window,
                onWindowChange = onWindowChange,
                axis = -1.0..1.0,
                decimals = 1,
                grid = GridStyle.EcgPaper,
                limit = extent,
                height = 200.dp,
                lineColor = MaterialTheme.colorScheme.primary,
                gridColor = MaterialTheme.colorScheme.outlineVariant,
                axisColor = MaterialTheme.colorScheme.outline,
                labelColor = MaterialTheme.colorScheme.onSurfaceVariant,
                setColor = MaterialTheme.colorScheme.primary.copy(alpha = 0.12f),
            )
        }
    }
}

/**
 * The waveform as it arrives, during a recording.
 *
 * These samples exist nowhere else: the watch streams them only while
 * something is showing them, and the stored copy only appears once the
 * recording finishes.
 */
@Composable
fun LiveEcgScreen(
    millivolts: List<Double>,
    samplingHz: Int,
    recording: Boolean,
    window: LongRange?,
    onWindowChange: (LongRange) -> Unit,
) {
    Column(
        Modifier.fillMaxSize().statusBarsPadding().padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Text(
            if (recording) "Recording" else "Recording complete",
            style = MaterialTheme.typography.headlineMedium,
        )
        val seconds = millivolts.size.toDouble() / samplingHz
        Text(
            String.format(
                Locale.US,
                "%.1f s · %d Hz · %s",
                seconds,
                samplingHz,
                if (recording) "live" else "pinch to zoom, drag to scroll",
            ),
            style = MaterialTheme.typography.bodySmall,
        )

        val step = 1000.0 / samplingHz
        // A window that slides with the newest sample, so it reads like paper
        // coming out of a machine rather than compressing as it goes.
        val span = (LIVE_ECG_SPAN_S * 1000).toLong()
        val end = (millivolts.size * step).toLong()
        // While it streams the view rides the newest sample; once it stops the
        // whole recording is there to be read, from the beginning.
        val follow = (end - span).coerceAtLeast(0L)
        val shown = window ?: if (recording) follow..(follow + span) else 0L..span
        val baseline = if (millivolts.isEmpty()) 0.0 else {
            millivolts.takeLast(samplingHz * 2).sorted().let { it[it.size / 2] }
        }
        ValueChart(
            points = millivolts.mapIndexed { index, mv ->
                ChartPoint((index * step).toLong(), mv - baseline)
            },
            markers = emptyList(),
            window = shown,
            onWindowChange = onWindowChange,
            axis = -1.0..1.0,
            decimals = 1,
            grid = GridStyle.EcgPaper,
            limit = 0L..end.coerceAtLeast(span),
            height = 220.dp,
            lineColor = MaterialTheme.colorScheme.primary,
            gridColor = MaterialTheme.colorScheme.outlineVariant,
            axisColor = MaterialTheme.colorScheme.outline,
            labelColor = MaterialTheme.colorScheme.onSurfaceVariant,
            setColor = MaterialTheme.colorScheme.primary.copy(alpha = 0.12f),
        )
        if (recording) {
            Text(
                "Keep the app open: the watch stops sending the waveform when " +
                    "nothing is showing it.",
                style = MaterialTheme.typography.bodySmall,
            )
        }
    }
}

private const val LIVE_ECG_SPAN_S = 6
