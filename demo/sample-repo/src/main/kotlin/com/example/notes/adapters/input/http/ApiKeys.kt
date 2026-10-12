package com.example.notes.adapters.input.http

import com.example.notes.domain.model.ClientKey
import org.http4k.core.Filter
import org.http4k.core.Request
import org.http4k.core.Status.Companion.UNAUTHORIZED
import org.http4k.core.with
import org.http4k.lens.Header
import org.http4k.lens.RequestKey

/** Known API keys. `internal` keys belong to internal service callers. */
data class ApiKeys(val external: Set<String>, val internal: Set<String> = emptySet()) {
    fun isKnown(key: String): Boolean = key in external || key in internal
}

val apiKeyHeader = Header.optional("X-API-Key")
val clientKeyLens = RequestKey.required<ClientKey>("client-key")

/** Rejects requests without a known API key and puts the caller's key on the request. */
fun requireApiKey(keys: ApiKeys): Filter = Filter { next ->
    { request: Request ->
        val key = apiKeyHeader(request)
        if (key == null || !keys.isKnown(key)) {
            errorResponse(UNAUTHORIZED, "unauthorized", "a valid X-API-Key header is required")
        } else {
            next(request.with(clientKeyLens of ClientKey(key)))
        }
    }
}
