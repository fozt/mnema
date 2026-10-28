package dev.mnema.app.launch

sealed interface LaunchResult {
    data object Started : LaunchResult
    data object NotPaired : LaunchResult
    data object NoWifi : LaunchResult
    data class Failed(val message: String) : LaunchResult
}

interface HelperLauncher {
    val paired: Boolean

    suspend fun launch(awaitNonce: suspend (String) -> Boolean): LaunchResult

    fun forget()
}
