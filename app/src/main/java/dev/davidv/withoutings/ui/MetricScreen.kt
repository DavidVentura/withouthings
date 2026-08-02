package dev.davidv.withoutings.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import java.util.Locale
import uniffi.wpp_ffi.Marker
import uniffi.wpp_ffi.Metric

/** How each series is drawn: its unit, precision, and the range it lives in. */
enum class MetricStyle(
    val metric: Metric,
    val label: String,
    val unit: String,
    val decimals: Int,
    /// Fixed y-axis for this series, in its own units.
    val axis: ClosedFloatingPointRange<Double>,
    /// How long a reading still describes now. Per metric because the watch
    /// measures respiratory rate barely once an hour: one horizon for all would
    /// call the slow ones stale for doing exactly what they do.
    val freshFor: Long,
) {
    HeartRate(Metric.HEART_RATE, "Heart rate", "bpm", 0, 50.0..150.0, 20 * MINUTE),
    Temperature(Metric.TEMPERATURE, "Temperature", "°C", 2, 35.0..40.0, 10 * MINUTE),
    HrvSdnn(Metric.HRV_SDNN, "HRV (SDNN)", "ms", 0, 0.0..200.0, 3 * HOUR),
    HrvRmssd(Metric.HRV_RMSSD, "HRV (RMSSD)", "ms", 0, 0.0..200.0, 3 * HOUR),
    Respiratory(Metric.RESPIRATORY_RATE, "Respiratory", "breaths/min", 0, 0.0..30.0, 4 * HOUR),
    Battery(Metric.BATTERY, "Battery", "%", 0, 0.0..100.0, HOUR),
    Steps(Metric.STEPS, "Steps", "per day", 0, 0.0..15000.0, DAY);

    /// One window for every series, so switching between them compares the
    /// same stretch of time rather than rescaling.
    val defaultSpan: Long get() = DEFAULT_SPAN

    companion object {
        fun of(metric: Metric) = entries.first { it.metric == metric }
    }
}

private const val MINUTE = 60_000L
private const val HOUR = 3600_000L
internal const val DAY = 24 * HOUR

private const val DEFAULT_SPAN = 6 * 3600_000L

/** Preset windows; anything else comes from pinching the chart. */
private val RANGES = listOf(
    "1h" to 3600_000L,
    "6h" to 6 * 3600_000L,
    "1d" to DAY,
    "7d" to 7 * DAY,
    "30d" to 30 * DAY,
)

@Composable
fun MetricScreen(
    style: MetricStyle,
    points: List<ChartPoint>,
    /// Shaded behind the trace: for the battery, when it was on a charger.
    markers: List<Marker>,
    window: LongRange,
    onWindowChange: (LongRange) -> Unit,
    onRange: (Long) -> Unit,
    onBack: () -> Unit,
) {
    Page(style.label, onBack) {
        // The series carries a point beyond each edge so the line can leave the
        // plot; they are not part of what is being shown.
        val visible = points.filter { it.atMs in window }
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            val latest = visible.maxByOrNull { it.atMs }?.value
            Stat("Latest", show(latest, style), style.unit, Modifier.weight(1f))
            Stat("Min", show(visible.minOfOrNull { it.value }, style), style.unit, Modifier.weight(1f))
            Stat("Max", show(visible.maxOfOrNull { it.value }, style), style.unit, Modifier.weight(1f))
        }

        // A pinched window sits between presets and selects none, so the
        // heading is the only thing that says where it is.
        val span = window.last - window.first
        Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Text("Window: ${spanLabel(span)}", style = MaterialTheme.typography.labelMedium)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                RANGES.forEach { (name, preset) ->
                    FilterChip(
                        selected = span == preset,
                        onClick = { onRange(preset) },
                        label = { Text(name) },
                    )
                }
            }
        }

        ValueChart(
            points = points,
            bands = markers.bands(MaterialTheme.colorScheme.primary.copy(alpha = 0.12f)),
            window = window,
            onWindowChange = onWindowChange,
            axis = style.axis,
            decimals = style.decimals,
            lineColor = MaterialTheme.colorScheme.primary,
            gridColor = MaterialTheme.colorScheme.outlineVariant,
            axisColor = MaterialTheme.colorScheme.outline,
            labelColor = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

private fun spanLabel(ms: Long): String {
    val minutes = ms / 60_000
    val days = minutes / (24 * 60)
    val hours = (minutes % (24 * 60)) / 60
    val rest = minutes % 60
    return when {
        minutes < 1 -> "${ms / 1000}s"
        days > 0 -> if (hours == 0L) "${days}d" else "${days}d${hours}h"
        hours > 0 -> if (rest == 0L) "${hours}h" else "${hours}h${rest}m"
        else -> "${minutes}m"
    }
}

private fun show(value: Double?, style: MetricStyle): String = when {
    value == null -> "—"
    style.decimals == 0 -> value.toInt().toString()
    else -> String.format(Locale.US, "%.${style.decimals}f", value)
}
