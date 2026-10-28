package dev.mnema.app.sync

import android.content.Context
import android.os.Environment
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import dev.mnema.app.MnemaApp
import java.util.concurrent.TimeUnit

class SyncWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        // Without All files access, other apps' files in the Mnema folder are invisible: a partial scan must not delete.
        if (Environment.isExternalStorageManager()) MnemaApp.graph(applicationContext).sync.runOnce()
        return Result.success()
    }
}

/** Events coalesce into one job. In the background — the minimal WorkManager period, no timer. */
class SyncScheduling(context: Context) {
    private val work = WorkManager.getInstance(context)
    private val online = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()
    fun wake() {
        work.enqueueUniqueWork(NOW, ExistingWorkPolicy.KEEP, OneTimeWorkRequestBuilder<SyncWorker>().setConstraints(online).build())
    }
    fun configure(enabled: Boolean) {
        if (enabled) {
            work.enqueueUniquePeriodicWork(PERIODIC, ExistingPeriodicWorkPolicy.KEEP,
                PeriodicWorkRequestBuilder<SyncWorker>(15, TimeUnit.MINUTES).setConstraints(online).build())
            wake()
        } else {
            work.cancelUniqueWork(NOW)
            work.cancelUniqueWork(PERIODIC)
        }
    }
    private companion object { const val NOW = "library-sync"; const val PERIODIC = "library-sync-periodic" }
}
