package dev.mnema.app.update

import android.content.pm.PackageInstaller
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class InstallOutcomeTest {
    private val otherKey = "Обновление подписано другим ключом. Нужен пакет с подписью установленной Mnema"
    private val aborted = InstallOutcome.Failure("Установка отменена")
    private val apk = File("/cache/updates/mnema-0.2.9.apk")
    private val manifest = UpdateManifest(
        version = "0.2.9",
        versionCode = 209,
        apk = "https://github.com/fozt/mnema/releases/download/v0.2.9/mnema-0.2.9.apk",
        sha256 = "c".repeat(64),
        size = 14_680_064,
        minSdk = 34,
    )

    private fun failure(status: Int, message: String? = null) =
        (installOutcome(status, message) as InstallOutcome.Failure).text

    @Test
    fun systemAsksTheOwnerToConfirm() {
        assertEquals(InstallOutcome.Confirm, installOutcome(PackageInstaller.STATUS_PENDING_USER_ACTION, null))
        assertEquals(InstallOutcome.Success, installOutcome(PackageInstaller.STATUS_SUCCESS, null))
    }

    @Test
    fun onlyAnExplicitSignatureMismatchExplainsTheSigningKey() {
        assertEquals("Система обнаружила конфликт при установке обновления", failure(PackageInstaller.STATUS_FAILURE_CONFLICT))
        assertEquals("Система обнаружила конфликт при установке обновления", failure(PackageInstaller.STATUS_FAILURE_CONFLICT, "INSTALL_FAILED_CONFLICTING_PROVIDER"))
        assertEquals(otherKey, failure(PackageInstaller.STATUS_FAILURE_CONFLICT, "signatures do not match"))
        assertEquals(
            otherKey,
            failure(PackageInstaller.STATUS_FAILURE, "INSTALL_FAILED_UPDATE_INCOMPATIBLE: signatures do not match"),
        )
    }

    @Test
    fun confirmationOfTheCurrentSessionAsksTheOwner() {
        val installing = UpdateStage.Installing(manifest, apk, 6)
        val confirm = UpdateStage.Confirm(manifest, apk, 6)
        val failed = UpdateStage.Failed("Установка отменена", manifest, apk)
        val cases = listOf<Triple<UpdateStage, InstallOutcome, UpdateStage>>(
            Triple(installing, InstallOutcome.Confirm, confirm),
            Triple(installing, aborted, failed),
            Triple(confirm, aborted, failed),
            Triple(confirm, InstallOutcome.Success, confirm),
        )

        for ((stage, outcome, expected) in cases) assertEquals("$stage, $outcome", expected, afterInstall(stage, 6, outcome))
    }

    @Test
    fun outcomeOfAnotherSessionIsIgnored() {
        assertNull(afterInstall(UpdateStage.Installing(manifest, apk, 6), 5, aborted))
        assertNull(afterInstall(UpdateStage.Installing(manifest, apk), 5, aborted))
        assertNull(afterInstall(UpdateStage.Confirm(manifest, apk, 6), 5, InstallOutcome.Confirm))
        assertNull(afterInstall(UpdateStage.Idle, 6, aborted))
        assertNull(afterInstall(UpdateStage.Failed("Нет связи"), 6, InstallOutcome.Confirm))
    }

    @Test
    fun repeatedInstallIsNotFailedByTheAbandonedSession() {
        var stage: UpdateStage = UpdateStage.Confirm(manifest, apk, 5)

        // the window did not open, "Install" again: the previous session is cancelled, the new number is not known yet
        stage = UpdateStage.Installing(manifest, apk)
        stage = afterInstall(stage, 5, aborted) ?: stage
        stage = UpdateStage.Installing(manifest, apk, 6)
        stage = afterInstall(stage, 5, aborted) ?: stage
        stage = afterInstall(stage, 6, InstallOutcome.Confirm) ?: stage

        assertEquals(UpdateStage.Confirm(manifest, apk, 6), stage)
    }
}
