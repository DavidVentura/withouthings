package dev.davidv.withoutings.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
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
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import dev.davidv.withoutings.ui.theme.AppTheme
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.math.roundToInt
import uniffi.wpp_ffi.UserProfile

private val birthFormat = SimpleDateFormat("d MMMM yyyy", Locale.getDefault())

/**
 * Who the watch thinks is wearing it.
 *
 * The watch is the store of record here, not this app: it holds the profile
 * and hands it back on request, and a write replaces the whole record. So the
 * page refuses to invent one — with nothing read back there is nothing to edit,
 * and writing a fresh record would zero the fields this app cannot see.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun WatchUserScreen(
    user: UserProfile?,
    saveState: SaveState,
    onApply: (birthSecs: Long, weightGrams: UInt, heightCm: UInt) -> Unit,
    onAcknowledge: () -> Unit,
    onBack: () -> Unit,
) {
    DetailScaffold(title = "Wearer", onBack = onBack) {
        Text(
            "Used by the watch to work out calories burned, gait length and " +
                "maximum heart rate.",
            style = AppTheme.type.bodyLarge,
            color = AppTheme.colors.onSurfaceTertiary,
        )

        if (user == null) {
            EmptyNote(
                "The watch has not reported a profile yet. It is sent on request " +
                    "once the link is up — sync, then come back."
            )
            return@DetailScaffold
        }

        var birthSecs by remember(user) { mutableStateOf(user.birthSecs) }
        var weightKg by remember(user) {
            mutableStateOf(String.format(Locale.US, "%.1f", user.weightGrams.toDouble() / 1000))
        }
        var heightCm by remember(user) { mutableStateOf(user.heightCm.toString()) }
        var picking by remember { mutableStateOf(false) }

        val weightGrams = weightKg.toDoubleOrNull()?.let { (it * 1000).roundToInt() }
            ?.takeIf { it in MIN_WEIGHT_G..MAX_WEIGHT_G }
        val height = heightCm.toIntOrNull()?.takeIf { it in MIN_HEIGHT_CM..MAX_HEIGHT_CM }
        val valid = weightGrams != null && height != null

        Column(
            Modifier.weight(1f).verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            if (user.firstName.isNotBlank()) {
                SettingRow("Name") {
                    Text(
                        user.firstName,
                        style = AppTheme.type.rowMeta,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                RowDivider(inset = 0.dp)
            }

            SettingRow("Date of birth") {
                OutlineAction(birthFormat.format(Date(birthSecs * 1000))) { picking = true }
            }
            RowDivider(inset = 0.dp)

            Row(
                Modifier.fillMaxWidth().padding(vertical = 8.dp),
                horizontalArrangement = Arrangement.spacedBy(10.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                OutlinedTextField(
                    value = weightKg,
                    onValueChange = { weightKg = it },
                    modifier = Modifier.weight(1f),
                    label = { Text("Weight") },
                    suffix = { Text("kg") },
                    singleLine = true,
                    isError = weightGrams == null,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                )
                OutlinedTextField(
                    value = heightCm,
                    onValueChange = { heightCm = it },
                    modifier = Modifier.weight(1f),
                    label = { Text("Height") },
                    suffix = { Text("cm") },
                    singleLine = true,
                    isError = height == null,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                )
            }
            if (!valid) {
                Text(
                    "Weight ${MIN_WEIGHT_G / 1000}–${MAX_WEIGHT_G / 1000} kg, " +
                        "height $MIN_HEIGHT_CM–$MAX_HEIGHT_CM cm.",
                    style = AppTheme.type.rowMeta,
                    color = AppTheme.colors.onSurfaceTertiary,
                )
            }
            Spacer(Modifier.height(16.dp))
        }

        val edited = birthSecs != user.birthSecs ||
            weightGrams?.toUInt() != user.weightGrams ||
            height?.toUInt() != user.heightCm
        SaveFooter(
            edited = edited,
            saveState = saveState,
            enabled = valid && edited,
            onAcknowledge = onAcknowledge,
            onSaved = onBack,
        ) {
            onApply(birthSecs, weightGrams!!.toUInt(), height!!.toUInt())
        }

        if (picking) {
            val picker = rememberDatePickerState(initialSelectedDateMillis = birthSecs * 1000)
            DatePickerDialog(
                onDismissRequest = { picking = false },
                confirmButton = {
                    TextButton(onClick = {
                        picker.selectedDateMillis?.let { birthSecs = it / 1000 }
                        picking = false
                    }) { Text("Set") }
                },
                dismissButton = {
                    TextButton(onClick = { picking = false }) { Text("Cancel") }
                },
            ) {
                DatePicker(picker)
            }
        }
    }
}

/// What the watch will take. Outside these the record is not a person, and a
/// write replaces the whole thing.
private const val MIN_WEIGHT_G = 20_000
private const val MAX_WEIGHT_G = 300_000
private const val MIN_HEIGHT_CM = 50
private const val MAX_HEIGHT_CM = 250
