package com.example.notes.adapters.input.http

import com.example.notes.domain.model.CreateNoteCommand
import com.example.notes.domain.model.CreateNoteResult
import com.example.notes.domain.model.Note
import com.example.notes.services.CreateNote
import com.example.notes.services.ListNotes
import org.http4k.core.Body
import org.http4k.core.Method.GET
import org.http4k.core.Method.POST
import org.http4k.core.Request
import org.http4k.core.Response
import org.http4k.core.Status.Companion.BAD_REQUEST
import org.http4k.core.Status.Companion.CREATED
import org.http4k.core.Status.Companion.OK
import org.http4k.core.then
import org.http4k.core.with
import org.http4k.format.Jackson.auto
import org.http4k.lens.LensFailure
import org.http4k.routing.RoutingHttpHandler
import org.http4k.routing.bind
import org.http4k.routing.routes

data class CreateNoteRequest(val title: String, val body: String = "")
data class NoteResponse(val id: String, val title: String, val body: String)
data class HealthResponse(val status: String)

private val createNoteLens = Body.auto<CreateNoteRequest>().toLens()
private val noteLens = Body.auto<NoteResponse>().toLens()
private val notesLens = Body.auto<List<NoteResponse>>().toLens()
private val healthLens = Body.auto<HealthResponse>().toLens()

private fun Note.toResponse() = NoteResponse(id.value, title, body)

fun noteRoutes(createNote: CreateNote, listNotes: ListNotes, apiKeys: ApiKeys): RoutingHttpHandler = routes(
    "/health" bind GET to { _: Request -> Response(OK).with(healthLens of HealthResponse("ok")) },
    "/notes" bind GET to { _: Request ->
        Response(OK).with(notesLens of listNotes.execute().map { it.toResponse() })
    },
    "/notes" bind POST to requireApiKey(apiKeys).then { request: Request ->
        val body = try {
            createNoteLens(request)
        } catch (e: LensFailure) {
            return@then errorResponse(BAD_REQUEST, "invalid_body", "expected JSON with a title")
        }
        when (val result = createNote.execute(CreateNoteCommand(clientKeyLens(request), body.title, body.body))) {
            is CreateNoteResult.Created -> Response(CREATED).with(noteLens of result.note.toResponse())
            is CreateNoteResult.Invalid -> errorResponse(BAD_REQUEST, "invalid_note", result.reason)
        }
    },
)
