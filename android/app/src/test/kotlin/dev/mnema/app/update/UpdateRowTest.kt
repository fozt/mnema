package dev.mnema.app.update

import dev.mnema.app.ui.settings.UpdateAction
import dev.mnema.app.ui.settings.UpdateRow
import dev.mnema.app.ui.settings.rowState
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class UpdateRowTest {
    private val apk = File("/cache/updates/mnema-0.2.9.apk")
    private val manifest = UpdateManifest(
        version = "0.2.9",
        versionCode = 209,
        apk = "https://github.com/fozt/mnema/releases/download/v0.2.9/mnema-0.2.9.apk",
        sha256 = "c".repeat(64),
        size = 14_680_064,
        minSdk = 34,
    )

    private fun row(stage: UpdateStage, recording: Boolean = false) = rowState("0.2.8", stage, recording)

    @Test
    fun newerVersionShowsItsSizeAndOffersAnUpdate() {
        val rows = listOf<Pair<UpdateStage, UpdateRow>>(
            UpdateStage.Idle to UpdateRow("Версия 0.2.8", null, UpdateAction.CHECK),
            UpdateStage.Checking to UpdateRow("Версия 0.2.8", "Проверяю…", null, busy = true),
            UpdateStage.UpToDate to UpdateRow("Версия 0.2.8", "Последняя версия", UpdateAction.CHECK),
            UpdateStage.Available(manifest) to UpdateRow("Версия 0.2.8", "Доступна версия 0.2.9 · 14 МБ", UpdateAction.UPDATE),
            UpdateStage.Unsuitable(manifest) to UpdateRow("Версия 0.2.8", "0.2.9 не подходит этому телефону", UpdateAction.CHECK),
            UpdateStage.Downloading(manifest, 0.43f) to UpdateRow("Версия 0.2.8", "Загружаю… 43 %", null, progress = 0.43f),
            UpdateStage.NeedsPermission(manifest, apk) to
                UpdateRow("Версия 0.2.8", "Разрешите Mnema устанавливать приложения", UpdateAction.ALLOW),
            UpdateStage.Ready(manifest, apk) to UpdateRow("Версия 0.2.8", "Загружена 0.2.9", UpdateAction.INSTALL),
            UpdateStage.Confirm(manifest, apk, 6) to UpdateRow("Версия 0.2.8", "Подтвердите установку", UpdateAction.INSTALL),
            UpdateStage.Installing(manifest, apk, 6) to UpdateRow("Версия 0.2.8", "Устанавливаю…", null, busy = true),
        )

        for ((stage, expected) in rows) assertEquals("$stage", expected, row(stage))
    }

    @Test
    fun failureRetriesTheStepThatFailed() {
        val failed = row(UpdateStage.Failed("Нет связи"))
        assertEquals(UpdateRow("Версия 0.2.8", "Нет связи", UpdateAction.CHECK_AGAIN), failed)
        assertEquals("Проверить снова", failed.action?.title)
        assertEquals(
            UpdateRow("Версия 0.2.8", "Файл обновления повреждён", UpdateAction.UPDATE),
            row(UpdateStage.Failed("Файл обновления повреждён", manifest)),
        )
        assertEquals(
            UpdateRow("Версия 0.2.8", "Установка отменена", UpdateAction.INSTALL),
            row(UpdateStage.Failed("Установка отменена", manifest, apk)),
        )
    }

    @Test
    fun updateAndInstallWaitForTheRecording() {
        val update = UpdateRow("Версия 0.2.8", "Можно после записи", UpdateAction.UPDATE, enabled = false)
        val install = UpdateRow("Версия 0.2.8", "Можно после записи", UpdateAction.INSTALL, enabled = false)
        assertEquals(update, row(UpdateStage.Available(manifest), recording = true))
        assertEquals(update, row(UpdateStage.Failed("Нет связи", manifest), recording = true))
        assertEquals(install, row(UpdateStage.Ready(manifest, apk), recording = true))
        assertEquals(install, row(UpdateStage.Confirm(manifest, apk, 6), recording = true))
        assertEquals(install, row(UpdateStage.Failed("Установка отменена", manifest, apk), recording = true))

        val notHeld = listOf(
            UpdateStage.Idle,
            UpdateStage.Checking,
            UpdateStage.Failed("Нет связи"),
            UpdateStage.NeedsPermission(manifest, apk),
            UpdateStage.Downloading(manifest, 0.43f),
        )
        for (stage in notHeld) assertEquals("$stage", row(stage), row(stage, recording = true))
    }

    @Test
    fun settingsTabCarriesADotWhileANewerVersionWaits() {
        val pending = listOf(
            UpdateStage.Available(manifest),
            UpdateStage.Downloading(manifest, 0.43f),
            UpdateStage.NeedsPermission(manifest, apk),
            UpdateStage.Ready(manifest, apk),
            UpdateStage.Installing(manifest, apk, 6),
            UpdateStage.Confirm(manifest, apk, 6),
            UpdateStage.Failed("Файл обновления повреждён", manifest),
        )
        val none = listOf(
            UpdateStage.Idle,
            UpdateStage.Checking,
            UpdateStage.UpToDate,
            UpdateStage.Unsuitable(manifest),
            UpdateStage.Failed("Нет связи"),
        )

        for (stage in pending) assertTrue("$stage", updatePending(stage))
        for (stage in none) assertFalse("$stage", updatePending(stage))
    }
}
