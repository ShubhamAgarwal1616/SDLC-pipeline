package {{package}}

import io.kotest.core.spec.style.DescribeSpec
import io.kotest.matchers.shouldBe
import io.kotest.matchers.string.shouldContain
import org.http4k.core.Method.GET
import org.http4k.core.Request
import org.http4k.core.Status.Companion.OK

class HealthTest : DescribeSpec({
    describe("{{name}}") {
        it("reports health") {
            val response = Dependencies().app(Request(GET, "/health"))
            response.status shouldBe OK
            response.bodyString() shouldContain "ok"
        }
    }
})
