package {{package}}

import org.http4k.core.HttpHandler
import org.http4k.core.Method.GET
import org.http4k.core.Response
import org.http4k.core.Status.Companion.OK
import org.http4k.routing.bind
import org.http4k.routing.routes

/** The only place where all layers meet (with Application.kt). */
class Dependencies {
    val app: HttpHandler = routes(
        "/health" bind GET to { Response(OK).header("Content-Type", "application/json").body("""{"status":"ok"}""") },
    )
}
