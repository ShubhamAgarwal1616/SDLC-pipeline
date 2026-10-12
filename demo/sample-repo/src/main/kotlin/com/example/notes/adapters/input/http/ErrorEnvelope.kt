package com.example.notes.adapters.input.http

import org.http4k.core.Body
import org.http4k.core.Response
import org.http4k.core.Status
import org.http4k.core.with
import org.http4k.format.Jackson.auto

/** The project's standard error envelope: {"error": {"code": "...", "message": "..."}} */
data class ErrorDetail(val code: String, val message: String)
data class ErrorEnvelope(val error: ErrorDetail)

private val errorLens = Body.auto<ErrorEnvelope>().toLens()

fun errorResponse(status: Status, code: String, message: String): Response =
    Response(status).with(errorLens of ErrorEnvelope(ErrorDetail(code, message)))
