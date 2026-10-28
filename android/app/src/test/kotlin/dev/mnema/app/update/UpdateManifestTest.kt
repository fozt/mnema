package dev.mnema.app.update

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.fail
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class UpdateManifestTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val apk = "https://github.com/fozt/mnema/releases/download/v0.2.8/mnema-0.2.8.apk"
    private val sha = "a".repeat(64)

    private fun manifest(
        version: String = "0.2.8",
        code: Long = 208,
        minSdk: Int = 34,
    ) = UpdateManifest(version = version, versionCode = code, apk = apk, sha256 = sha, size = 14_680_064, minSdk = minSdk)

    private fun json(vararg fields: Pair<String, String>): String {
        val all = linkedMapOf(
            "version" to "\"0.2.8\"",
            "version_code" to "20008",
            "apk" to "\"$apk\"",
            "sha256" to "\"$sha\"",
            "size" to "14680064",
            "min_sdk" to "34",
        )
        fields.forEach { (key, value) -> if (value.isEmpty()) all.remove(key) else all[key] = value }
        return all.entries.joinToString(",", "{", "}") { (key, value) -> "\"$key\": $value" }
    }

    private fun rejected(text: String) {
        try {
            parseManifest(text)
        } catch (e: UpdateException) {
            assertEquals("Не удалось проверить обновления. Повторите позже", e.message)
            return
        }
        fail("manifest accepted: $text")
    }

    @Test
    fun manifestFromTheRegistryIsReadAndExtraFieldsAreIgnored() {
        val read = parseManifest(json("notes" to "\"новое\"", "channel" to "{\"name\": \"main\"}"))

        assertEquals(
            UpdateManifest(version = "0.2.8", versionCode = 20008, apk = apk, sha256 = sha, size = 14_680_064, minSdk = 34),
            read,
        )
    }

    @Test
    fun badValuesAreRejected() {
        listOf("version", "version_code", "apk", "sha256", "size", "min_sdk").forEach { rejected(json(it to "")) }
        rejected("не json")
        rejected("[]")
        rejected(json("version" to "\"../../0.2.8\""))
        rejected(json("version" to "\"0.2\""))
        rejected(json("version_code" to "0"))
        rejected(json("apk" to "\"http://github.com/mnema-0.2.8.apk\""))
        rejected(json("apk" to "\"https://user:pass@github.com/mnema-0.2.8.apk\""))
        rejected(json("sha256" to "\"${"A".repeat(64)}\""))
        rejected(json("sha256" to "\"abc\""))
        rejected(json("size" to "0"))
        rejected(json("size" to "${1L shl 40}"))
        rejected(json("min_sdk" to "0"))
    }

    @Test
    fun newerVersionIsAvailable() {
        val newer = manifest(code = 209)
        val same = manifest(code = 208)
        val older = manifest(code = 207)
        val forNewerAndroid = manifest(code = 300, minSdk = 37)
        val cases = listOf(
            Triple(newer, 36, UpdateStage.Available(newer)),
            Triple(same, 36, UpdateStage.UpToDate),
            Triple(older, 36, UpdateStage.UpToDate),
            Triple(forNewerAndroid, 36, UpdateStage.Unsuitable(forNewerAndroid)),
            Triple(forNewerAndroid, 37, UpdateStage.Available(forNewerAndroid)),
        )

        for ((offered, sdk, expected) in cases) {
            assertEquals("${offered.versionCode}, sdk $sdk", expected, decide(installedCode = 208, manifest = offered, sdk = sdk))
        }
    }

    @Test
    fun foundVersionOutlivesARestart() {
        val file = File(folder.root, "update-manifest.json")
        val newer = manifest(code = 209)

        saveManifest(file, manifest(code = 208))
        saveManifest(file, newer)

        assertEquals(newer, loadManifest(file))
        assertEquals(UpdateStage.Available(newer), decide(installedCode = 208, manifest = loadManifest(file)!!, sdk = 36))
        assertEquals(listOf(file.name), folder.root.list()?.toList())
    }

    @Test
    fun missingOrBrokenSavedManifestIsNothing() {
        val file = File(folder.root, "update-manifest.json")

        assertNull(loadManifest(file))
        file.writeText("{\"version\": \"0.2.9\"}")
        assertNull(loadManifest(file))
        file.writeText("не json")
        assertNull(loadManifest(file))
    }
}
