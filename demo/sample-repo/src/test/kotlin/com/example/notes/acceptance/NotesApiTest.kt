package com.example.notes.acceptance

import com.example.notes.Dependencies
import com.example.notes.adapters.input.http.ApiKeys
import io.kotest.core.spec.style.DescribeSpec
import io.kotest.matchers.shouldBe
import io.kotest.matchers.string.shouldContain
import org.http4k.core.Method.GET
import org.http4k.core.Method.POST
import org.http4k.core.Request
import org.http4k.core.Status.Companion.BAD_REQUEST
import org.http4k.core.Status.Companion.CREATED
import org.http4k.core.Status.Companion.OK
import org.http4k.core.Status.Companion.UNAUTHORIZED

class NotesApiTest : DescribeSpec({
    fun app() = Dependencies(ApiKeys(external = setOf("key-1"), internal = setOf("internal-1"))).app

    describe("notes API") {
        it("reports health") {
            val response = app()(Request(GET, "/health"))
            response.status shouldBe OK
            response.bodyString() shouldContain "ok"
        }

        it("lists notes without an API key") {
            app()(Request(GET, "/notes")).status shouldBe OK
        }

        it("creates a note with a valid API key, then lists it") {
            val app = app()
            val created = app(Request(POST, "/notes").header("X-API-Key", "key-1").body("""{"title":"Hello"}"""))
            created.status shouldBe CREATED
            app(Request(GET, "/notes")).bodyString() shouldContain "Hello"
        }

        it("accepts an internal API key") {
            app()(Request(POST, "/notes").header("X-API-Key", "internal-1").body("""{"title":"x"}""")).status shouldBe CREATED
        }

        it("rejects a write without an API key using the error envelope") {
            val response = app()(Request(POST, "/notes").body("""{"title":"Hello"}"""))
            response.status shouldBe UNAUTHORIZED
            response.bodyString() shouldContain "\"code\":\"unauthorized\""
        }

        it("rejects an unknown API key") {
            app()(Request(POST, "/notes").header("X-API-Key", "nope").body("""{"title":"x"}""")).status shouldBe UNAUTHORIZED
        }

        it("rejects an invalid note") {
            val response = app()(Request(POST, "/notes").header("X-API-Key", "key-1").body("""{"title":"  "}"""))
            response.status shouldBe BAD_REQUEST
            response.bodyString() shouldContain "invalid_note"
        }

        it("rejects a malformed body") {
            app()(Request(POST, "/notes").header("X-API-Key", "key-1").body("not json")).status shouldBe BAD_REQUEST
        }
    }
})
