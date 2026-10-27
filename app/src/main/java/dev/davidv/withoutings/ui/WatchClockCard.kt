package dev.davidv.withoutings.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Refresh
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.TextButton
import androidx.compose.material3.TimeInput
import androidx.compose.material3.TimePicker
import androidx.compose.material3.TimePickerState
import androidx.compose.material3.rememberTimePickerState
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import dev.davidv.withoutings.ui.theme.AppTheme
import kotlinx.coroutines.delay
import java.time.Instant
import java.time.ZoneId
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import kotlin.math.abs
import uniffi.wpp_ffi.ClockReadState
import uniffi.wpp_ffi.ClockSetState
import uniffi.wpp_ffi.WatchClockView

private val SECONDS = DateTimeFormatter.ofPattern("HH:mm:ss")
private val MINUTES = DateTimeFormatter.ofPattern("HH:mm")

/**
 * The watch's clock beside the phone's. The watch's time is only ever what it
 * answered to a time request, carried forward on the phone's clock since; with
 * no answer the card says so and falls back to when the phone last set it.
 */
@Composable
fun WatchClockCard(
    clock: WatchClockView?,
    setAskedHere: Boolean,
    canSet: Boolean,
    onSet: () -> Unit,
    onRefresh: () -> Unit,
    onSetCustom: (Long) -> Unit = {},
    now: () -> Long = System::currentTimeMillis,
    zone: ZoneId = ZoneId.systemDefault(),
) {
    var nowMs by remember { mutableLongStateOf(now()) }
    LaunchedEffect(Unit) {
        while (true) {
            nowMs = now()
            delay(1_000 - nowMs % 1_000)
        }
    }
    val phoneOffset = zone.rules.getOffset(Instant.ofEpochMilli(nowMs)).totalSeconds
    val watchUtc = clock?.watchUtcMs
    val readAt = clock?.readAtMs
    val watchOffset = clock?.watchGmtOffsetSeconds

    val enabled = canRead(clock)
    Column(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(AppTheme.radius.card))
            .background(MaterialTheme.colorScheme.surfaceContainer)
            .padding(start = 12.dp, end = 4.dp, top = 6.dp, bottom = 8.dp),
        verticalArrangement = Arrangement.spacedBy(2.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            ClockFigure(
                "Watch",
                if (watchUtc != null && readAt != null && watchOffset != null) {
                    clockText(watchUtc + (nowMs - readAt), watchOffset)
                } else {
                    "--:--:--"
                },
                Modifier.weight(1f),
            )
            ClockFigure("Phone", clockText(nowMs, phoneOffset), Modifier.weight(1f))
            GlyphButton(Icons.Rounded.Refresh, "Read the watch's time again", enabled = enabled, size = 20.dp) {
                onRefresh()
            }
        }
        Text(
            clockLine(clock, setAskedHere, phoneOffset, nowMs, zone),
            Modifier.fillMaxWidth().padding(end = 8.dp),
            style = AppTheme.type.rowMeta.copy(fontWeight = FontWeight.Medium),
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
        val setEnabled = canSet && clock?.setState != ClockSetState.SENDING
        var picking by remember { mutableStateOf(false) }
        Row(
            Modifier.fillMaxWidth().padding(top = 4.dp, end = 8.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            SetPill("Set from phone", setEnabled, Modifier.weight(1f), onClick = onSet)
            SetPill("Custom time", setEnabled, Modifier.weight(1f), onClick = { picking = true })
        }
        if (picking) {
            CustomTimeDialog(
                nowMs = now(),
                zone = zone,
                onDismiss = { picking = false },
                onPick = { shift ->
                    picking = false
                    onSetCustom(shift)
                },
            )
        }
    }
}

/**
 * Picks a watch time in the phone's zone. The result is how far it is from
 * the phone's clock, taken the nearest way round (within 12 hours), so 00:10
 * picked at 23:50 is twenty minutes ahead, not a day behind.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun CustomTimeDialog(nowMs: Long, zone: ZoneId, onDismiss: () -> Unit, onPick: (Long) -> Unit) {
    val context = LocalContext.current
    val start = remember { Instant.ofEpochMilli(nowMs).atZone(zone) }
    val state = rememberTimePickerState(
        initialHour = start.hour,
        initialMinute = start.minute,
        is24Hour = android.text.format.DateFormat.is24HourFormat(context),
    )
    val roomy = LocalConfiguration.current.screenHeightDp >= 600
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Watch time", style = AppTheme.type.rowTitle) },
        text = { CustomTimeBody(state, start.offset.totalSeconds, roomy) },
        confirmButton = {
            TextButton(onClick = {
                val picked = start.withHour(state.hour).withMinute(state.minute)
                onPick(nearest(picked.toInstant().toEpochMilli() - start.toInstant().toEpochMilli()))
            }) { Text("Set") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

/** The picker and its zone note; the dial when there is room, else typed fields. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun CustomTimeBody(state: TimePickerState, gmtOffsetSeconds: Int, roomy: Boolean) {
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        if (roomy) TimePicker(state) else TimeInput(state)
        Text(
            "Phone zone, ${gmt(gmtOffsetSeconds)}",
            style = AppTheme.type.rowMeta,
            color = AppTheme.colors.onSurfaceTertiary,
        )
    }
}

private const val HALF_DAY_MS = 12 * 3_600_000L

private fun nearest(shiftMs: Long): Long {
    var s = shiftMs % (2 * HALF_DAY_MS)
    if (s > HALF_DAY_MS) s -= 2 * HALF_DAY_MS
    if (s <= -HALF_DAY_MS) s += 2 * HALF_DAY_MS
    return s
}

/** The longer story behind the card, for the ⓘ panel: one fact a line. */
fun clockDetailLines(
    clock: WatchClockView?,
    nowMs: Long,
    zone: ZoneId = ZoneId.systemDefault(),
): List<String> {
    val phoneOffset = zone.rules.getOffset(Instant.ofEpochMilli(nowMs)).totalSeconds
    val lines = mutableListOf<String>()
    val readAt = clock?.readAtMs
    lines += if (readAt != null) {
        "Watch time: as the watch said it, ${ago(nowMs - readAt)}"
    } else {
        "Watch time: not read yet"
    }
    clock?.watchGmtOffsetSeconds?.let { lines += "Zone: watch ${gmt(it)}, phone ${gmt(phoneOffset)}" }
    lines += syncLine(clock, nowMs, zone)
    if ((clock?.customShiftMs ?: 0L) != 0L) {
        lines += "Custom time kept on reconnects until Set from phone"
    }
    lines += "Both sets use the phone's zone"
    return lines
}

@Composable
private fun SetPill(label: String, enabled: Boolean, modifier: Modifier = Modifier, onClick: () -> Unit) {
    Text(
        label,
        modifier
            .clip(AppTheme.pill)
            .background(
                if (enabled) MaterialTheme.colorScheme.primaryContainer else MaterialTheme.colorScheme.surfaceContainerHigh
            )
            .clickableIf(enabled, onClick)
            .padding(horizontal = 12.dp, vertical = 8.dp),
        style = AppTheme.type.buttonLabel.copy(fontSize = 13.sp),
        color = if (enabled) MaterialTheme.colorScheme.onPrimaryContainer else AppTheme.colors.onSurfaceDim,
        textAlign = TextAlign.Center,
        maxLines = 1,
        overflow = TextOverflow.Ellipsis,
    )
}

@Composable
private fun ClockFigure(label: String, value: String, modifier: Modifier) {
    Column(modifier) {
        Text(label, style = AppTheme.type.rowMeta, color = AppTheme.colors.onSurfaceTertiary, maxLines = 1)
        Text(
            value,
            style = TextStyle(
                fontFamily = FontFamily.Default,
                fontSize = 20.sp,
                lineHeight = 24.sp,
                fontWeight = FontWeight.SemiBold,
                letterSpacing = 0.sp,
            ),
            color = MaterialTheme.colorScheme.onSurface,
            maxLines = 1,
        )
    }
}

private fun Modifier.clickableIf(enabled: Boolean, onClick: () -> Unit): Modifier =
    if (enabled) clickable(onClick = onClick) else this

private fun canRead(clock: WatchClockView?) =
    clock?.readState != ClockReadState.ASKING && clock?.readState != ClockReadState.WAITING_FOR_WATCH

private fun clockText(utcMs: Long, offsetSeconds: Int): String =
    Instant.ofEpochMilli(utcMs).atOffset(ZoneOffset.ofTotalSeconds(offsetSeconds)).format(SECONDS)

private fun gmt(offsetSeconds: Int): String {
    if (offsetSeconds == 0) return "GMT"
    val sign = if (offsetSeconds > 0) "+" else "−"
    val m = abs(offsetSeconds) / 60
    return if (m % 60 == 0) "GMT$sign${m / 60}" else "GMT$sign${m / 60}:${"%02d".format(m % 60)}"
}

private fun ago(ms: Long): String {
    val s = (ms / 1000).coerceAtLeast(0)
    return when {
        s < 5 -> "just now"
        s < 60 -> "${s} s ago"
        s < 3600 -> "${s / 60} min ago"
        else -> "${s / 3600} h ago"
    }
}

/** Whole seconds only: the watch counts nothing finer. */
private fun difference(offsetMs: Long): String {
    val s = Math.round(offsetMs / 1000.0)
    return when {
        abs(s) <= 1 -> "In sync"
        s > 0 -> "${span(s)} ahead"
        else -> "${span(-s)} behind"
    }
}

private fun span(seconds: Long): String = when {
    seconds < 60 -> "$seconds s"
    seconds < 3600 -> "${seconds / 60} min"
    else -> "${seconds / 3600} h ${(seconds % 3600) / 60} min"
}

/** The one line under the times: a set in progress first, else the difference. */
private fun clockLine(
    clock: WatchClockView?,
    setAskedHere: Boolean,
    phoneOffset: Int,
    nowMs: Long,
    zone: ZoneId,
): String {
    when (clock?.setState) {
        ClockSetState.WAITING_FOR_HANDS -> return "Sets after Done/Cancel"
        ClockSetState.WAITING_FOR_WATCH -> return "Sets when connected"
        ClockSetState.SENDING -> return "Setting…"
        ClockSetState.SET -> if (setAskedHere) return "Set · recheck hands"
        ClockSetState.UNANSWERED -> if (setAskedHere) return "Not confirmed"
        ClockSetState.REFUSED -> return "Refused (error ${clock.setErr})"
        else -> {}
    }
    val offset = clock?.offsetMs
    val zones = clock?.watchGmtOffsetSeconds?.takeIf { it != phoneOffset }?.let { " · ${gmt(it)}" } ?: ""
    val custom = if ((clock?.customShiftMs ?: 0L) != 0L) " · custom" else ""
    val reading = offset?.let { difference(it) + zones + custom }
    val lastSet = clock?.lastSyncAtMs?.let { " · set ${Instant.ofEpochMilli(it).atZone(zone).format(MINUTES)}" } ?: ""
    return when (clock?.readState) {
        null, ClockReadState.NEVER, ClockReadState.WAITING_FOR_WATCH -> "Waiting for the watch"
        ClockReadState.ASKING -> reading ?: "Asking the watch…"
        ClockReadState.ANSWERED -> reading ?: "No time in the answer"
        ClockReadState.UNANSWERED -> "No answer$lastSet"
        ClockReadState.REFUSED -> "Can't read (error ${clock.readErr})"
    }
}

private fun syncLine(clock: WatchClockView?, nowMs: Long, zone: ZoneId): String {
    val at = clock?.lastSyncAtMs ?: return "Last set: not since the app started"
    val time = Instant.ofEpochMilli(at).atZone(zone).format(MINUTES)
    val who = if (clock.lastSyncFromScreen) "here" else "on connecting"
    val answered = if (clock.lastSyncAnswered) "" else ", not confirmed"
    return "Last set: $time (${ago(nowMs - at)}), $who$answered"
}
