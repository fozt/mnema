package dev.mnema.app.i18n

import java.util.Locale

/** Interface language. Russian when the system or app language is Russian, English otherwise. */
enum class UiLang(val tag: String) {
    EN("en"),
    RU("ru");

    companion object {
        fun of(language: String?): UiLang = if (language?.lowercase(Locale.ROOT)?.startsWith("ru") == true) RU else EN
    }
}

/**
 * Process-wide interface language. MnemaApp sets it from the configuration locale, which already
 * includes the per-app language chosen in Android settings.
 */
object Ui {
    @Volatile
    var lang: UiLang = UiLang.of(Locale.getDefault().language)
}
