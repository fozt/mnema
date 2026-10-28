package dev.mnema.app.ui.components

/** A form check error at the [field] it belongs to. */
data class FieldError<F>(val field: F, val text: String)

/** The error text if it belongs to [field]; another field or no error gives null. */
fun <F> FieldError<F>?.at(field: F): String? = this?.takeIf { it.field == field }?.text
