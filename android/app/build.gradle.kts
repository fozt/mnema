import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
}

// Release version: -PmnemaVersion=X.Y.Z, versionCode = X*1000000 + Y*10000 + Z.
val mnemaVersion: String? = providers.gradleProperty("mnemaVersion").orNull
val mnemaVersionCode: Int = mnemaVersion?.let { version ->
    val (major, minor, patch) = Regex("""(0|[1-9][0-9]*)\.(0|[1-9][0-9]?)\.(0|[1-9][0-9]{0,3})""")
        .matchEntire(version)?.destructured?.toList()?.map(String::toInt)
        ?: error("mnemaVersion must look like X.Y.Z, Y below 100, Z below 10000: $version")
    major * 1_000_000 + minor * 10_000 + patch
} ?: 1
val releaseKeystore: String? = providers.environmentVariable("MNEMA_KEYSTORE").orNull?.takeIf { it.isNotEmpty() }

fun signingEnv(name: String): String =
    providers.environmentVariable(name).orNull?.takeIf { it.isNotEmpty() }
        ?: error("Signing with the MNEMA_KEYSTORE key needs the variable $name")

android {
    namespace = "dev.mnema.app"
    compileSdk = 37
    buildToolsVersion = "37.0.0"

    defaultConfig {
        applicationId = "dev.mnema.app"
        minSdk = 34
        targetSdk = 36
        versionCode = mnemaVersionCode
        versionName = mnemaVersion ?: "0.0.0"
    }

    signingConfigs {
        // The permanent release key; without MNEMA_KEYSTORE release is signed with the debug key.
        if (releaseKeystore != null) {
            create("release") {
                storeFile = file(releaseKeystore)
                storePassword = signingEnv("MNEMA_KEYSTORE_PASSWORD")
                keyAlias = signingEnv("MNEMA_KEY_ALIAS")
                keyPassword = signingEnv("MNEMA_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            isDebuggable = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = signingConfigs.findByName("release") ?: signingConfigs.getByName("debug")
        }
    }

    sourceSets.getByName("test").resources.srcDir("../../tests/fixtures")

    // On macOS /var is a symlink to /private/var: tests compare paths, so the temp folder has no symlinks
    val testTmpdir = File(System.getProperty("java.io.tmpdir")).canonicalPath
    testOptions.unitTests.all {
        it.systemProperty("java.io.tmpdir", testTmpdir)
        // Existing tests assert Russian texts
        it.systemProperty("user.language", "ru")
    }

    buildFeatures {
        compose = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}

dependencies {
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.material3)
    implementation(libs.androidx.compose.material.icons.extended)
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.navigation.compose)
    implementation(libs.androidx.datastore.preferences)
    implementation(libs.androidx.work.runtime.ktx)
    implementation(libs.androidx.media3.exoplayer)
    implementation(libs.kotlinx.coroutines.android)
    implementation(libs.kotlinx.serialization.json)
    implementation(libs.okhttp)
    implementation(libs.kadb)

    testImplementation(libs.junit)
    testImplementation(libs.kotlinx.coroutines.test)
    testImplementation(libs.okhttp.mockwebserver3)
}
