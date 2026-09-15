package dev.davidv.withoutings.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.collectAsState
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import dev.davidv.withoutings.ui.theme.AppTheme
import kotlinx.coroutines.delay
import java.io.File

private const val DUMP_FILE = "external_flash.bin"

@Composable
fun FlashDumpCard(model: WatchViewModel, modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val state by model.flashDump.collectAsState()
    val file = remember(context) { File(context.getExternalFilesDir(null), DUMP_FILE) }
    val dumping = state is FlashDumpState.Dumping

    Column(
        modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(AppTheme.radius.card))
            .background(MaterialTheme.colorScheme.surfaceContainer)
            .padding(horizontal = 15.dp, vertical = 13.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        Eyebrow("debug · external flash")

        Row(
            Modifier.height(IntrinsicSize.Min),
            horizontalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            FilledAction(
                "Test flash read",
                Modifier.weight(1f).fillMaxHeight(),
                shape = AppTheme.pill,
                container = MaterialTheme.colorScheme.secondaryContainer,
                content = MaterialTheme.colorScheme.onSecondaryContainer,
                enabled = !dumping,
                onClick = { model.testFlashRead() },
            )
            if (dumping) {
                OutlineAction(
                    "Cancel dump",
                    Modifier.weight(1f).fillMaxHeight(),
                    onClick = { model.cancelFlashDump() },
                )
            } else {
                FilledAction(
                    "Dump full flash",
                    Modifier.weight(1f).fillMaxHeight(),
                    shape = AppTheme.pill,
                    onClick = { model.dumpFlash(file) },
                )
            }
        }

        var nowMs by remember { mutableLongStateOf(System.currentTimeMillis()) }
        LaunchedEffect(dumping) {
            while (dumping) {
                nowMs = System.currentTimeMillis()
                delay(1000)
            }
        }

        StatusText(state, file, nowMs)
    }
}

@Composable
private fun StatusText(state: FlashDumpState, file: File, nowMs: Long) {
    val text = when (state) {
        is FlashDumpState.Idle ->
            "Reads the watch's external SPI flash over BLE.\nadb pull ${file.absolutePath}"
        is FlashDumpState.Testing -> state.message
        is FlashDumpState.Dumping -> {
            val percent = if (state.total == 0L) 0 else (state.done * 100 / state.total)
            "Dumping ${state.done} / ${state.total} bytes ($percent%)\n" +
                progressLine(state, nowMs) + "\nadb pull ${state.path}"
        }
        is FlashDumpState.Done -> state.message
        is FlashDumpState.Failed -> state.reason
    }
    Spacer(Modifier.height(0.dp))
    Text(
        text,
        style = AppTheme.type.rowMeta,
        color = AppTheme.colors.onSurfaceTertiary,
    )
}

// ETA extrapolates from this run's own throughput, so it ignores bytes a resume
// skipped and reads correctly on a fresh dump and a resumed one alike.
private fun progressLine(state: FlashDumpState.Dumping, nowMs: Long): String {
    val elapsedMs = (nowMs - state.startedAtMs).coerceAtLeast(0)
    val doneThisRun = state.done - state.startOffset
    val eta = if (doneThisRun > 0 && elapsedMs > 0) {
        fmtDuration((state.total - state.done) * elapsedMs / doneThisRun)
    } else {
        "—"
    }
    return "elapsed ${fmtDuration(elapsedMs)} · ETA $eta"
}

private fun fmtDuration(ms: Long): String {
    val s = ms / 1000
    return if (s >= 60) "${s / 60}m ${s % 60}s" else "${s}s"
}
