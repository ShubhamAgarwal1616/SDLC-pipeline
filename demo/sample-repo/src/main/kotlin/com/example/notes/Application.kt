package com.example.notes

import com.example.notes.adapters.input.http.ApiKeys
import org.http4k.server.SunHttp
import org.http4k.server.asServer

fun main() {
    fun keys(name: String) = System.getenv(name).orEmpty().split(",").map { it.trim() }.filter { it.isNotEmpty() }.toSet()
    val port = System.getenv("PORT")?.toIntOrNull() ?: 8080
    val apiKeys = ApiKeys(external = keys("API_KEYS"), internal = keys("INTERNAL_API_KEYS"))
    Dependencies(apiKeys).app.asServer(SunHttp(port)).start()
    println("notes-api listening on :$port")
}
