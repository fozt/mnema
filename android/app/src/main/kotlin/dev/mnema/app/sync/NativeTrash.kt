package dev.mnema.app.sync

import android.content.Context
import dev.mnema.app.i18n.tr
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import java.io.File
import java.io.IOException
import java.util.UUID
import kotlin.io.path.ExperimentalPathApi
import kotlin.io.path.deleteRecursively

/** The Android 17 trash. The API has no support check, so a system refusal is remembered until the process restarts. */
class NativeTrash internal constructor(
    private val api: Int,
    private val allFiles: () -> Boolean,
    private val probe: () -> File,
    private val trashFile: (File) -> Unit,
) {
    constructor(context: Context, root: File) : this(
        Build.VERSION.SDK_INT,
        { Environment.isExternalStorageManager() },
        { File(root, ".mnema-trash-check-${UUID.randomUUID()}") },
        { file -> if (Build.VERSION.SDK_INT >= 37) MediaStore.trashFile(context.contentResolver, file.absolutePath) },
    )

    @Volatile private var supported: Boolean? = null

    /** Not on the main thread. Without All files access nothing can be deleted forever: the trash refuses with an access request and erases nothing. */
    fun available(): Boolean = api >= 37 && (!allFiles() || (supported ?: check()))

    fun move(file: File) {
        if (api < 37) throw SyncException(tr("The trash requires Android 17"))
        if (!allFiles()) throw SyncException(tr("Allow All files access to move recordings to the trash"))
        if (supported == false) throw SyncException(NO_TRASH)
        try { trashFile(file) }
        catch (_: SecurityException) { throw SyncException(tr("No access to the trash. Allow All files access")) }
        catch (_: UnsupportedOperationException) { supported = false; throw SyncException(NO_TRASH) }
        catch (_: IllegalStateException) { throw SyncException(NOT_TRASHED) }
        catch (_: IllegalArgumentException) { throw SyncException(NOT_TRASHED) }
        if (file.exists()) throw SyncException(NOT_TRASHED)
    }

    // a path that does not exist: without support the system refuses at once, with it — because of the path
    private fun check(): Boolean = try {
        trashFile(probe())
        true.also { supported = it }
    } catch (_: UnsupportedOperationException) { false.also { supported = it } }
    catch (_: SecurityException) { false }
    catch (_: Exception) { true.also { supported = it } }
}

/** With a trash the recording goes there; without one it is deleted forever, only if the user agreed. */
internal fun discard(file: File, forever: Boolean, hasTrash: Boolean, trash: (File) -> Unit) = when {
    hasTrash -> trash(file)
    forever -> deleteForever(file)
    else -> throw SyncException(NO_TRASH)
}

/** Deletes the whole folder; a link inside is removed itself, its target stays. */
@OptIn(ExperimentalPathApi::class)
internal fun deleteForever(file: File) {
    try { file.toPath().deleteRecursively() } catch (_: IOException) { throw SyncException(NOT_DELETED) }
}

internal val NO_TRASH: String get() = tr("The trash is unavailable")
private val NOT_TRASHED: String get() = tr("The system did not move the recording to the trash")
private val NOT_DELETED: String get() = tr("Could not delete the recording files")
