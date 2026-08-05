package dev.davidv.withoutings

import android.content.Context
import android.os.Environment
import java.io.File

sealed interface DbLocation {
    data class Ready(val path: String) : DbLocation
    data object NeedsAllFiles : DbLocation
}

// Uninstall erases every app-private path, Android/data and Android/media
// included, and retained data keeps the old signing certificate so a release
// build cannot install over a debug one's. Shared storage is the only place a
// database outlives either.
private val SHARED_DIR
    get() = File(
        Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOCUMENTS),
        "withoutings",
    )

fun watchDb(context: Context): DbLocation {
    if (!Environment.isExternalStorageManager()) return DbLocation.NeedsAllFiles

    val dir = SHARED_DIR
    val shared = File(dir, "watch.db")
    if (shared.exists()) return DbLocation.Ready(shared.absolutePath)

    check(dir.exists() || dir.mkdirs()) { "cannot create ${dir.absolutePath}" }

    val internal = context.getDatabasePath("watch.db")
    if (!internal.exists()) return DbLocation.Ready(shared.absolutePath)

    // The -wal holds committed transactions the main file does not, so moving
    // one without the other silently drops everything since the last checkpoint.
    val wal = File("${internal.path}-wal")
    internal.copyTo(shared)
    if (wal.exists()) wal.copyTo(File("${shared.path}-wal"))

    check(internal.renameTo(File("${internal.path}.moved"))) { "cannot set aside $internal" }
    if (wal.exists()) check(wal.renameTo(File("${wal.path}.moved"))) { "cannot set aside $wal" }
    File("${internal.path}-shm").delete()

    return DbLocation.Ready(shared.absolutePath)
}
