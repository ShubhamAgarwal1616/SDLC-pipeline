package com.example.notesclient

import com.example.notesclient.adapters.http.NotesClient
import com.example.notesclient.domain.CreateNoteResult
import com.example.notesclient.domain.ListNotesResult
import io.kotest.core.spec.style.DescribeSpec
import io.kotest.matchers.shouldBe
import io.kotest.matchers.types.shouldBeInstanceOf
import org.http4k.core.HttpHandler
import org.http4k.core.Method.GET
import org.http4k.core.Method.POST
import org.http4k.core.Request
import org.http4k.core.Response
import org.http4k.core.Status.Companion.CREATED
import org.http4k.core.Status.Companion.OK
import org.http4k.core.Status.Companion.UNAUTHORIZED
import org.http4k.routing.bind
import org.http4k.routing.routes

/** A tiny stand-in for notes-api, so the client is tested through real HTTP messages. */
private fun fakeNotesApi(): HttpHandler {
    val notes = mutableListOf<String>()
    return routes(
        "/notes" bind GET to { _: Request ->
            Response(OK).body(notes.joinToString(",", "[", "]") { """{"id":"$it","title":"$it","body":""}""" })
        },
        "/notes" bind POST to { req: Request ->
            if (req.header("X-API-Key") != "key-1") {
                Response(UNAUTHORIZED).body("""{"error":{"code":"unauthorized","message":"a valid X-API-Key header is required"}}""")
            } else {
                notes += "n${notes.size + 1}"
                Response(CREATED).body("""{"id":"n${notes.size}","title":"Hello","body":""}""")
            }
        },
    )
}

class NotesClientTest : DescribeSpec({
    describe("NotesClient") {
        it("creates and lists notes") {
            val client = NotesClient(fakeNotesApi(), "key-1")
            client.create("Hello").shouldBeInstanceOf<CreateNoteResult.Created>()
            (client.list() as ListNotesResult.Listed).notes.size shouldBe 1
        }

        it("maps the API error envelope on rejection") {
            val result = NotesClient(fakeNotesApi(), "wrong").create("Hello")
            result shouldBe CreateNoteResult.Rejected(401, "unauthorized", "a valid X-API-Key header is required")
        }

        it("lists an empty set") {
            (NotesClient(fakeNotesApi(), "key-1").list() as ListNotesResult.Listed).notes shouldBe emptyList()
        }
    }
})
