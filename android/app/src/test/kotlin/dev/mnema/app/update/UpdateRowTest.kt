package dev.mnema.app.update

import dev.mnema.app.ui.settings.UpdateAction
import dev.mnema.app.ui.settings.UpdateRow
import dev.mnema.app.ui.settings.rowState
import java.io.File
import java.time.LocalDate
import java.time.ZoneId
import java.time.ZoneOffset
import org.junit.Assert.assertEquals
import org.junit.Test

class UpdateRowTest {
    private val zone: ZoneId = ZoneOffset.UTC
    private val today: LocalDate = LocalDate.of(2026, 10, 22)
    private val noon = today.atTime(12, 0).toInstant(ZoneOffset.UTC).toEpochMilli()
    private val apk = File("/cache/updates/mnema-0.2.9.apk")
    private val manifest = UpdateManifest(
        version = "0.2.9",
        versionCode = 209,
        apk = "https://github.com/fozt/mnema/releases/download/v0.2.9/mnema-0.2.9.apk",
        sha256 = "c".repeat(64),
        size = 14_680_064,
        minSdk = 34,
    )

    private fun row(stage: UpdateStage, checkedAt: Long = 0, recording: Boolean = false) =
        rowState("0.2.8", stage, checkedAt, today, zone, recording)

    @Test
    fun lastCheckDayIsShown() {
        assertEquals("Проверено сегодня", row(UpdateStage.Idle, checkedAt = noon).subtitle)
        assertEquals("Проверено вчера", row(UpdateStage.Idle, checkedAt = noon - 24 * 3_600_000L).subtitle)
        assertEquals("Проверено сегодня", row(UpdateStage.UpToDate, checkedAt = noon).subtitle)
        assertEquals(UpdateAction.CHECK, row(UpdateStage.UpToDate, checkedAt = noon).action)
    }

    @Test
    fun newerVersionShowsItsSizeAndOffersAnUpdate() {
        val rows = listOf<Pair<UpdateStage, UpdateRow>>(
            UpdateStage.Idle to UpdateRow("Версия 0.2.8", null, UpdateAction.CHECK),
            UpdateStage.Checking to UpdateRow("Версия 0.2.8", "Проверяю…", null, busy = true),
            UpdateStage.Available(manifest) to UpdateRow("Версия 0.2.8", "Доступна 0.2.9 · 14 МБ", UpdateAction.UPDATE),
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
        assertEquals(UpdateRow("Версия 0.2.8", "Нет связи", UpdateAction.CHECK), row(UpdateStage.Failed("Нет связи")))
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
        for (stage in notHeld) assertEquals("$stage", row(stage, checkedAt = noon), row(stage, checkedAt = noon, recording = true))
    }
}
