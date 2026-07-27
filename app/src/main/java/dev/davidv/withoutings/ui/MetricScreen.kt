package dev.davidv.withoutings.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import java.util.Locale
import uniffi.wpp_ffi.Metric

/** How each series is drawn: its unit, precision, and a sensible axis floor. */
enum class MetricStyle(
    val metric: Metric,
    val label: String,
    val unit: String,
    val decimals: Int,
    val minSpan: Double,
    /// Window to open on. A daily total needs weeks to show anything; a 1 Hz
    /// series needs hours.
    val defaultSpan: Long,
) {
    HeartRate(Metric.HEART_RATE, "Heart rate", "bpm", 0, 30.0, DAY),
    Temperature(Metric.TEMPERATURE, "Temperature", "°C", 2, 1.0, DAY),
    HrvSdnn(Metric.HRV_SDNN, "HRV (SDNN)", "ms", 0, 20.0, 7 * DAY),
    HrvRmssd(Metric.HRV_RMSSD, "HRV (RMSSD)", "ms", 0, 20.0, 7 * DAY),
    Respiratory(Metric.RESPIRATORY_RATE, "Respiratory", "breaths/min", 0, 6.0, DAY),
    Battery(Metric.BATTERY, "Battery", "%", 0, 20.0, 7 * DAY),
    Steps(Metric.STEPS, "Steps", "per day", 0, 1000.0, 30 * DAY);

    companion object {
        fun of(metric: Metric) = entries.first { it.metric == metric }
    }
}

internal const val DAY = 24 * 3600_000L

/** Preset windows; anything else comes from pinching the chart. */
private val RANGES = listOf(
    "1h" to 3600_000L,
    "6h" to 6 * 3600_000L,
    "24h" to DAY,
    "7d" to 7 * DAY,
    "30d" to 30 * DAY,
)

@Composable
fun MetricScreen(
    style: MetricStyle,
    points: List<ChartPoint>,
    window: LongRange,
    onWindowChange: (LongRange) -> Unit,
    onRange: (Long) -> Unit,
) {
    Column(
        Modifier.fillMaxSize().statusBarsPadding().padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text(style.label, style = MaterialTheme.typography.headlineMedium)

        val latest = points.maxByOrNull { it.atMs }?.value
        val summary = if (points.isEmpty()) "no data in this window" else {
            val min = points.minOf { it.value }
            val max = points.maxOf { it.value }
            val mean = points.sumOf { it.value } / points.size
            "${points.size} points · " +
                "min ${format(min, style)} · " +
                "mean ${format(mean, style)} · " +
                "max ${format(max, style)}"
        }
        Text(summary, style = MaterialTheme.typography.bodySmall)

        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Stat("Latest", latest?.let { format(it, style) } ?: "—", style.unit, Modifier.weight(1f))
        }

        ValueChart(
            points = points,
            markers = emptyList(),
            window = window,
            onWindowChange = onWindowChange,
            minSpan = style.minSpan,
            decimals = style.decimals,
            lineColor = MaterialTheme.colorScheme.primary,
            gridColor = MaterialTheme.colorScheme.outlineVariant,
            axisColor = MaterialTheme.colorScheme.outline,
            labelColor = MaterialTheme.colorScheme.onSurfaceVariant,
            setColor = MaterialTheme.colorScheme.primary.copy(alpha = 0.12f),
        )

        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            RANGES.forEach { (name, span) ->
                Button(onClick = { onRange(span) }) { Text(name) }
            }
        }
    }
}

private fun format(value: Double, style: MetricStyle): String =
    if (style.decimals == 0) value.toInt().toString()
    else String.format(Locale.US, "%.${style.decimals}f", value)
