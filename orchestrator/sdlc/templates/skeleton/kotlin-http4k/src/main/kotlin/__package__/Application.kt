package {{package}}

import org.http4k.server.SunHttp
import org.http4k.server.asServer

fun main() {
    val port = System.getenv("PORT")?.toIntOrNull() ?: 8080
    Dependencies().app.asServer(SunHttp(port)).start()
    println("{{name}} listening on :$port")
}
