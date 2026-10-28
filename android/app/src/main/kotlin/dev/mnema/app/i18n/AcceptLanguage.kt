package dev.mnema.app.i18n

import okhttp3.Interceptor
import okhttp3.Response

/** Asks the server to answer (errors, remarks) in the interface language of the moment. */
object AcceptLanguage : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response =
        chain.proceed(chain.request().newBuilder().header("Accept-Language", Ui.lang.tag).build())
}
