# Http4k Patterns Reference

All Http4k code lives in `adapters/input/http/`. Routes implement route handlers; lenses provide
type-safe request/response extraction; filters handle cross-cutting concerns. HTTP routes call
**services directly** — there is no input port for the HTTP side (ARCH-11). Never use exceptions for
control flow (HTTP-02). Always use lenses — never raw string parsing (HTTP-01).

Because services are **synchronous by default** (CORO-00), handlers call `service.execute(...)`
directly. There is no `runBlocking` bridge.

---

## Routes

### Basic route handler

```kotlin
// adapters/input/http/ProductRoutes.kt
class ProductRoutes(
    private val getProduct: GetProduct,
    private val createProduct: CreateProduct
) {

    private val productIdLens = Path.string().of("productId")
    private val productResponseLens = Body.auto<ProductResponse>().toLens()
    private val createProductRequestLens = Body.auto<CreateProductRequest>().toLens()
    private val errorResponseLens = Body.auto<ErrorResponse>().toLens()

    fun routes(): RoutingHttpHandler = routes(
        "/products/{productId}" bind Method.GET to ::getProductHandler,
        "/products" bind Method.POST to ::createProductHandler
    )

    private fun getProductHandler(request: Request): Response {
        val productId = ProductId(productIdLens(request))
        return when (val result = getProduct.execute(productId)) {
            is GetProductResult.Success ->
                Response(Status.OK).with(productResponseLens of result.product.toResponse())
            is GetProductResult.NotFound ->
                Response(Status.NOT_FOUND).with(errorResponseLens of ErrorResponse("Product not found: ${productId.value}"))
        }
    }

    private fun createProductHandler(request: Request): Response {
        val command = createProductRequestLens(request).toCommand()
        return when (val result = createProduct.execute(command)) {
            is CreateProductResult.Success ->
                Response(Status.CREATED).with(productResponseLens of result.product.toResponse())
            is CreateProductResult.ValidationFailed ->
                Response(Status.BAD_REQUEST).with(errorResponseLens of ErrorResponse(result.reason))
            is CreateProductResult.DuplicateProduct ->
                Response(Status.CONFLICT).with(errorResponseLens of ErrorResponse("Product already exists"))
        }
    }
}
```

**If a service is `suspend`** (only when justified — CORO-00), bridge with Http4k's coroutine
integration at the framework level, not with `runBlocking` in each handler. `runBlocking` must never
appear in handlers (CORO-04).

---

## Lenses

Lenses are type-safe extractors/injectors for HTTP request and response parts. Define them once
as properties; reuse across handlers.

### Request body lens (JSON)

```kotlin
val createOrderRequestLens = Body.auto<CreateOrderRequest>().toLens()

// Extraction in handler:
val request: CreateOrderRequest = createOrderRequestLens(httpRequest)
```

### Path parameter lens

```kotlin
val orderIdLens = Path.string().of("orderId")
val quantityLens = Path.int().of("quantity")

// Extraction:
val orderId = OrderId(orderIdLens(httpRequest))
val quantity = quantityLens(httpRequest)
```

### Query parameter lens

```kotlin
val pageLens = Query.int().defaulting("page", 0)
val filterLens = Query.string().optional("status")

// Extraction:
val page = pageLens(httpRequest)
val statusFilter = filterLens(httpRequest)  // String?
```

### Response lens injection

```kotlin
val orderResponseLens = Body.auto<OrderResponse>().toLens()

Response(Status.CREATED).with(orderResponseLens of order.toResponse())
```

### Lens failure handling

Lenses throw `LensFailure` on extraction failure. Catch at the filter level, not in individual
handlers:

```kotlin
// In a filter applied to all routes:
Filter { next ->
    { request ->
        try {
            next(request)
        } catch (e: LensFailure) {
            Response(Status.BAD_REQUEST).with(errorLens of ErrorResponse("Invalid request: ${e.message}"))
        }
    }
}
```

---

## Filters

Filters are applied via `then()`. They wrap handlers to add cross-cutting behaviour.

### Filter chain composition

```kotlin
// Application.kt
private val mainHandler: HttpHandler =
    ServerFilters.OpenTelemetryTracing(dependencies.openTelemetry)
        .then(ServerFilters.CatchLensFailure { lensFailure ->
            Response(Status.BAD_REQUEST).with(errorLens of ErrorResponse(lensFailure.message ?: "Bad request"))
        })
        .then(RequestContextFilter)
        .then(ApiHandler(dependencies))
```

### Custom filter example — request logging

```kotlin
fun requestLoggingFilter(logger: Logger): Filter = Filter { next ->
    { request ->
        val start = System.currentTimeMillis()
        next(request).also { response ->
            val duration = System.currentTimeMillis() - start
            logger.info("${request.method} ${request.uri} -> ${response.status.code} in ${duration}ms")
        }
    }
}
```

### Custom filter example — authentication

```kotlin
fun authFilter(verifyToken: (String) -> AuthResult): Filter = Filter { next ->
    { request ->
        val token = authHeaderLens(request).removePrefix("Bearer ")
        when (val result = verifyToken(token)) {
            is AuthResult.Valid -> next(request.with(principalLens of result.principal))
            is AuthResult.Invalid -> Response(Status.UNAUTHORIZED)
            AuthResult.Missing -> Response(Status.UNAUTHORIZED)
        }
    }
}
```

---

## ApiHandler and routing composition

```kotlin
// adapters/input/http/ApiHandler.kt
fun ApiHandler(dependencies: Dependencies): HttpHandler = routes(
    "/health" bind Method.GET to { Response(Status.OK) },
    "/api/v1" bind routes(
        ProductRoutes(
            getProduct = dependencies.getProduct,
            createProduct = dependencies.createProduct
        ).routes(),
        OrderRoutes(
            createOrder = dependencies.createOrder,
            getOrder = dependencies.getOrder
        ).routes()
    )
)
```

---

## HTTP models and mapping

HTTP request/response models live only in `adapters/input/http/`. Map to/from domain at the route
handler boundary.

```kotlin
// adapters/input/http/CreateOrderRequest.kt
data class CreateOrderRequest(
    val customerId: String,
    val items: List<OrderItemRequest>
)

data class OrderItemRequest(val productId: String, val quantity: Int)

fun CreateOrderRequest.toCommand() = CreateOrderCommand(
    customerId = CustomerId(customerId),
    items = items.map { OrderItem(ProductId(it.productId), it.quantity) }
)

// adapters/input/http/OrderResponse.kt
data class OrderResponse(val orderId: String, val status: String, val totalPence: Long)

fun Order.toResponse() = OrderResponse(
    orderId = id.value,
    status = status.name,
    totalPence = total.pence
)
```

HTTP models **never** cross into `services/` or `domain/`. Map at the route handler boundary.

---

## Error response model

```kotlin
// adapters/input/http/ErrorResponse.kt
data class ErrorResponse(val message: String, val code: String? = null)
```

Use a shared lens for error responses:

```kotlin
// adapters/input/http/CommonLenses.kt
val errorResponseLens = Body.auto<ErrorResponse>().toLens()
```
