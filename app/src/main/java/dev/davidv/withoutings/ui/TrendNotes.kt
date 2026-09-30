package dev.davidv.withoutings.ui

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.DeleteOutline
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.DatePicker
import androidx.compose.material3.DatePickerDialog
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberDatePickerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import dev.davidv.withoutings.ui.theme.AppTheme
import uniffi.wpp_ffi.Note
import java.util.Calendar
import java.util.TimeZone

@Composable
fun NotesList(notes: List<Note>, onAdd: (Long, String) -> Unit, onDelete: (Long) -> Unit) {
    var adding by remember { mutableStateOf(false) }
    var deleting by remember { mutableStateOf<Note?>(null) }

    Column(Modifier.fillMaxWidth()) {
        SectionHeader("Notes", "Add") { adding = true }
        if (notes.isEmpty()) {
            EmptyNote("A note marks a day on every chart, a change of medication or an illness, so what came after it can be read against what came before.")
        }
        notes.sortedByDescending { it.atMs }.forEachIndexed { index, note ->
            if (index > 0) RowDivider(inset = 0.dp)
            Row(Modifier.fillMaxWidth().padding(vertical = 4.dp), verticalAlignment = Alignment.CenterVertically) {
                Text(
                    dayAndMonth(note.atMs),
                    Modifier.width(96.dp),
                    style = AppTheme.type.rowMeta,
                    color = AppTheme.colors.onSurfaceTertiary,
                )
                Text(note.text, Modifier.weight(1f), style = AppTheme.type.rowTitle)
                GlyphButton(Icons.Rounded.DeleteOutline, "Delete this note") { deleting = note }
            }
        }
    }

    if (adding) {
        AddNoteDialog(onDismiss = { adding = false }, onAdd = { atMs, text -> adding = false; onAdd(atMs, text) })
    }
    val target = deleting ?: return
    AlertDialog(
        onDismissRequest = { deleting = null },
        containerColor = MaterialTheme.colorScheme.surfaceContainer,
        title = { Text("Delete this note?") },
        text = { Text("${dayAndMonth(target.atMs)} · ${target.text}") },
        confirmButton = { TextButton(onClick = { deleting = null; onDelete(target.id) }) { Text("Delete") } },
        dismissButton = { TextButton(onClick = { deleting = null }) { Text("Cancel") } },
    )
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun AddNoteDialog(onDismiss: () -> Unit, onAdd: (Long, String) -> Unit) {
    var text by remember { mutableStateOf("") }
    var picking by remember { mutableStateOf(false) }
    val today = dayStart(System.currentTimeMillis())
    var dayMs by remember { mutableStateOf(today) }

    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = MaterialTheme.colorScheme.surfaceContainer,
        title = { Text("Add a note") },
        text = {
            Column {
                TextButton(onClick = { picking = true }) { Text(fullDate(dayMs)) }
                OutlinedTextField(text, { text = it }, label = { Text("What changed") }, singleLine = true)
            }
        },
        confirmButton = {
            TextButton(onClick = { onAdd(dayMs, text.trim()) }, enabled = text.isNotBlank()) { Text("Add") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )

    if (!picking) return
    val picker = rememberDatePickerState(initialSelectedDateMillis = utcMidnightOf(dayMs))
    DatePickerDialog(
        onDismissRequest = { picking = false },
        confirmButton = {
            TextButton(onClick = {
                picker.selectedDateMillis?.let { dayMs = localDayOf(it) }
                picking = false
            }) { Text("OK") }
        },
        dismissButton = { TextButton(onClick = { picking = false }) { Text("Cancel") } },
    ) { DatePicker(picker) }
}

// The picker speaks in UTC midnights, and a note belongs to the local day the
// readings are keyed by, so the date crosses between the two by its fields.
private fun localDayOf(utcMidnightMs: Long): Long {
    val utc = Calendar.getInstance(TimeZone.getTimeZone("UTC")).apply { timeInMillis = utcMidnightMs }
    return Calendar.getInstance().apply {
        clear()
        set(utc.get(Calendar.YEAR), utc.get(Calendar.MONTH), utc.get(Calendar.DAY_OF_MONTH))
    }.timeInMillis
}

private fun utcMidnightOf(localDayMs: Long): Long {
    val local = Calendar.getInstance().apply { timeInMillis = localDayMs }
    return Calendar.getInstance(TimeZone.getTimeZone("UTC")).apply {
        clear()
        set(local.get(Calendar.YEAR), local.get(Calendar.MONTH), local.get(Calendar.DAY_OF_MONTH))
    }.timeInMillis
}
