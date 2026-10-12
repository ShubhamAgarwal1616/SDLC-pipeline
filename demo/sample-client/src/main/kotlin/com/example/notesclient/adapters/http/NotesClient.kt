package com.example.notesclient.adapters.http

import com.example.notesclient.domain.CreateNoteResult
import com.example.notesclient.domain.ListNotesResult
import com.example.notesclient.domain.Note
import org.http4k.core.Body
import org.http4k.core.HttpHandler
import org.http4k.core.Method.GET
import org.http4k.core.Method.POST
import org.http4k.core.Request
import org.http4k.core.Response
import org.http4k.core.Status.Companion.CREATED
import org.http4k.core.Status.Companion.OK
import org.http4k.core.with
import org.http4k.format.Jackson.auto
import org.http4k.lens.LensFailure

private data class NoteDto(val id: String, val title: String, val body: String)
private data class CreateNoteDto(val title: String, val body: String)
private data class ErrorDetail(val code: String, val message: String)
private data class ErrorEnvelope(val error: ErrorDetail)

private val notesLens = Body.auto<List<NoteDto>>().toLens()
private val noteLens = Body.auto<NoteDto>().toLens()
private val createLens = Body.auto<CreateNoteDto>().toLens()
private val errorLens = Body.auto<ErrorEnvelope>().toLens()

/** Client for notes-api. `http` is any Http4k handler (a real HTTP client, or the app itself in tests). */
class NotesClient(private val http: HttpHandler, private val apiKey: String) {

    fun list(): ListNotesResult {
        val response = http(Request(GET, "/notes"))
        return if (response.status == OK) {
            ListNotesResult.Listed(notesLens(response).map { Note(it.id, it.title, it.body) })
        } else {
            val (code, message) = error(response)
            ListNotesResult.Failed(response.status.code, code, message)
        }
    }

    fun create(title: String, body: String = ""): CreateNoteResult {
        val response = http(Request(POST, "/notes").header("X-API-Key", apiKey).with(createLens of CreateNoteDto(title, body)))
        return if (response.status == CREATED) {
            noteLens(response).let { CreateNoteResult.Created(Note(it.id, it.title, it.body)) }
        } else {
            val (code, message) = error(response)
            CreateNoteResult.Rejected(response.status.code, code, message)
        }
    }

    private fun error(response: Response): Pair<String, String> = try {
        errorLens(response).error.let { it.code to it.message }
    } catch (e: LensFailure) {
        "unexpected_response" to response.bodyString().take(200)
    }
}
