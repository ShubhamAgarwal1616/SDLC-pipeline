# Anti-Patterns Reference

Every anti-pattern below includes wrong code, corrected code, and the rule ID it violates.

---

## Idiomatic Kotlin — prefer these (positive patterns)

Before the "don'ts", here is what idiomatic code in this codebase looks like. Reviews should nudge
toward these, not only flag violations.

### Expression bodies + scope functions

```kotlin
// Prefer: expression body, scope function for the intermediate value
fun execute(productId: ProductId): GetProductResult =
    productRepository.findById(productId)
        ?.let { GetProductResult.Success(it) }
        ?: GetProductResult.NotFound(productId)

// Chaining a dependent step reads top-to-bottom with `let`:
fun execute(command: CreateOrderCommand): CreateOrderResult =
    productRepository.findById(command.productId)
        ?.let { product -> orderRepository.save(Order.from(command, product)) }
        ?.let { CreateOrderResult.Success(it) }
        ?: CreateOrderResult.ProductNotFound(command.productId)
```

- `let` — transform a value / scope a non-null result.
- `also` — side effect on a value while passing it through (e.g. cache write, logging).
- `apply` — configure a builder/mutable object and return it.
- `run` / `with` — group operations on a receiver.

Use them to remove temporaries and `!!`, not to build deep unreadable chains. If a chain stops being
readable, extract a named `val` or a private function.

### Collection operators over loops

```kotlin
// Prefer
val total = items.sumOf { it.price }
val names = products.map { it.name }
val inStock = products.filter { it.stockCount > 0 }
```

### Null handling without `!!`

```kotlin
val product = productRepository.findById(id) ?: return GetProductResult.NotFound(id)   // elvis early return
val name = product?.name ?: "unknown"                                                   // elvis default
val config = requireNotNull(loadConfig()) { "config missing" }                          // checked, with message
```

### Value objects validate their own invariants

A value object enforces its constraints in `init` and throws on violation — a bad value is a
programmer error, not an expected outcome (ERR-04):

```kotlin
@JvmInline
value class ProductId(val value: String) {
    init { require(value.isNotBlank()) { "ProductId must not be blank" } }
}

data class Money(val pence: Long) {
    init { require(pence >= 0) { "Money cannot be negative, was $pence" } }
}
```

`require`/`check` throwing here is correct. Reserve sealed result types for *expected* domain
outcomes (not found, validation failed) — never for guarding against bad construction.

---

## Java-isms in Kotlin

### STYLE-01 — `!!` operator (error)

**Wrong:**
```kotlin
fun getProductName(id: ProductId): String {
    val product = productRepository.findById(id)
    return product!!.name  // NullPointerException waiting to happen
}
```

**Correct:**
```kotlin
fun getProductName(id: ProductId): GetProductNameResult =
    productRepository.findById(id)
        ?.let { GetProductNameResult.Success(it.name) }
        ?: GetProductNameResult.NotFound(id)
```

`!!` is forbidden (severity: error). Use `?.`, `?:`, `requireNotNull`/`checkNotNull` with a message,
or a sealed result type to handle absence explicitly.

---

### STYLE-02 — `var` and mutable Java collections

**Wrong:**
```kotlin
var items = ArrayList<OrderItem>()
items.add(OrderItem(productId, quantity))
var total = 0.0
for (item in items) { total += item.price }
```

**Correct:**
```kotlin
val items = listOf(OrderItem(productId, quantity))
val total = items.sumOf { it.price }
```

---

### STYLE-03 — Inheritance hierarchies instead of sealed classes

**Wrong:**
```kotlin
open class OrderResult
class SuccessResult(val order: Order) : OrderResult()
class FailureResult(val reason: String) : OrderResult()
// non-exhaustive, cannot use smart cast
```

**Correct:**
```kotlin
sealed interface CreateOrderResult {
    data class Success(val order: Order) : CreateOrderResult
    data class ValidationFailed(val reason: String) : CreateOrderResult
}

val response = when (result) {
    is CreateOrderResult.Success -> Response(Status.CREATED)
    is CreateOrderResult.ValidationFailed -> Response(Status.BAD_REQUEST)
    // compiler enforces exhaustiveness
}
```

---

### STYLE-04 — Companion object as static utility dumping ground

**Wrong:**
```kotlin
class OrderUtils {
    companion object {
        fun formatOrderId(id: String) = "ORD-$id"
        fun calculateTax(amount: Double) = amount * 0.2
    }
}
```

**Correct:**
```kotlin
// top-level functions in the relevant package
fun formatOrderId(id: String) = "ORD-$id"
fun calculateTax(amount: Double) = amount * 0.2
```

---

### STYLE-05 — Imperative temporaries instead of scope functions

**Wrong:**
```kotlin
fun execute(customerId: CustomerId, region: String): PricingResult {
    val catalogue = catalogueRepository.findActive(region)
    val price = priceRepository.findFor(customerId, region, catalogue)
    return price
}
```

**Correct:**
```kotlin
fun execute(customerId: CustomerId, region: String): PricingResult =
    catalogueRepository.findActive(region).let { catalogue ->
        priceRepository.findFor(customerId, region, catalogue)
    }
```

---

### ERR-04 / STYLE — try-catch for control flow

**Wrong:**
```kotlin
fun findProduct(id: String): Product {
    return try {
        productRepository.findById(ProductId(id))!!
    } catch (e: NullPointerException) {
        throw ProductNotFoundException(id)
    }
}
```

**Correct:**
```kotlin
fun findProduct(id: ProductId): FindProductResult =
    productRepository.findById(id)
        ?.let { FindProductResult.Success(it) }
        ?: FindProductResult.NotFound(id)
```

---

## Coroutines: don't reach for them by default

### CORO-00 — Needless `suspend`

**Wrong — `suspend` with no asynchrony, forcing coroutines on every caller and test:**
```kotlin
interface ProductRepository {
    suspend fun findById(id: ProductId): Product?  // nothing here benefits from suspend
}

class GetProduct(private val repo: ProductRepository) {
    suspend fun execute(id: ProductId): GetProductResult = /* ... */   // suspends with no reason
}
```

**Correct — plain by default; add `suspend` only when there is a real concurrency/IO benefit:**
```kotlin
interface ProductRepository {
    fun findById(id: ProductId): Product?
}

class GetProduct(private val repo: ProductRepository) {
    fun execute(id: ProductId): GetProductResult = /* ... */
}
```

Reach for `suspend`/coroutines for fan-out concurrency, non-blocking IO under high concurrency, or
streaming — and then apply the CORO rules below.

---

### CORO-02 — Blocking GCP SDK calls in a suspend function

Applies **once you have decided** an adapter should be `suspend`.

**Wrong:**
```kotlin
override suspend fun findById(id: ProductId): Product? {
    // Blocks the coroutine thread — starves the dispatcher
    val doc = firestore.collection("products").document(id.value).get().get()
    return doc.toObject(ProductDocument::class.java)?.toDomain()
}
```

**Correct:**
```kotlin
override suspend fun findById(id: ProductId): Product? =
    withContext(Dispatchers.IO) {
        firestore.collection("products").document(id.value).get().get()
            .toObject(ProductDocument::class.java)?.toDomain()
    }
```

(If the repository is synchronous instead — the default — the blocking `.get().get()` call on the
request thread is fine and `withContext` is unnecessary.)

---

### CORO-03 — `Thread.sleep()` in coroutine code

**Wrong:**
```kotlin
suspend fun retryWithBackoff(block: suspend () -> Unit) {
    repeat(3) {
        block()
        Thread.sleep(1000)  // Blocks the thread
    }
}
```

**Correct:**
```kotlin
suspend fun retryWithBackoff(block: suspend () -> Unit) {
    repeat(3) {
        block()
        delay(1000)
    }
}
```

---

### CORO-04 — `runBlocking` inside suspend functions, services, or Http4k handlers

**Wrong:**
```kotlin
val handler: HttpHandler = { request ->
    val result = runBlocking { createOrder.execute(command) }  // blocks; and unnecessary if service is sync
    Response(Status.OK)
}
```

**Correct (synchronous service — no bridge at all):**
```kotlin
val handler: HttpHandler = { request ->
    val result = createOrder.execute(command)
    Response(Status.OK)
}
```

`runBlocking` is permitted **only** at a non-coroutine SDK callback boundary (e.g. a Pub/Sub
`MessageReceiver`), never deep in services or domain code.

---

### CORO-05 — `GlobalScope`

**Wrong:**
```kotlin
fun start() {
    GlobalScope.launch {  // Not tied to any lifecycle; leaks on shutdown
        consumer.start()
    }
}
```

**Correct:**
```kotlin
// In Application.kt — scope is tied to the application lifecycle
private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

fun start(): Application {
    scope.launch { dependencies.orderEventConsumer.start() }
    return this
}

private fun onShutdown() {
    scope.cancel()
}
```

---

## Hexagonal / DDD violations

### ARCH-01 — GCP imports inside `domain/`

**Wrong:**
```kotlin
// domain/model/Product.kt
import com.google.cloud.firestore.annotation.DocumentId  // Firestore in domain!

data class Product(
    @DocumentId val id: String,
    val name: String
)
```

**Correct:**
```kotlin
// domain/model/Product.kt
data class Product(val id: ProductId, val name: String)

// adapters/output/firestore/ProductDocument.kt
data class ProductDocument(val id: String = "", val name: String = "")
fun ProductDocument.toDomain() = Product(ProductId(id), name)
```

---

### ARCH-08 — Jackson annotations on domain classes

**Wrong:**
```kotlin
// domain/model/Order.kt
import com.fasterxml.jackson.annotation.JsonProperty  // Jackson in domain!

data class Order(
    @JsonProperty("order_id") val id: OrderId,
    val items: List<OrderItem>
)
```

**Correct:**
```kotlin
// domain/model/Order.kt
data class Order(val id: OrderId, val items: List<OrderItem>)

// adapters/input/http/OrderResponse.kt
data class OrderResponse(
    @JsonProperty("order_id") val id: String,
    val items: List<OrderItemResponse>
)
fun Order.toResponse() = OrderResponse(id = id.value, items = items.map { it.toResponse() })
```

---

### ARCH-06 — Business logic in route handlers

**Wrong:**
```kotlin
// adapters/input/http/OrderRoutes.kt
val createOrder: HttpHandler = { request ->
    val orderRequest = orderLens(request)
    // Business logic leaking into the handler!
    if (orderRequest.items.isEmpty()) {
        Response(Status.BAD_REQUEST).with(errorLens of ErrorResponse("Order must have items"))
    } else {
        val product = productRepository.findById(ProductId(orderRequest.items.first().productId))
        // ... more business logic ...
        Response(Status.CREATED)
    }
}
```

**Correct:**
```kotlin
// adapters/input/http/OrderRoutes.kt
val createOrderHandler: HttpHandler = { request ->
    val command = orderLens(request).toCommand()
    when (val result = createOrder.execute(command)) {
        is CreateOrderResult.Success -> Response(Status.CREATED).with(orderLens of result.order.toResponse())
        is CreateOrderResult.ValidationFailed -> Response(Status.BAD_REQUEST).with(errorLens of ErrorResponse(result.reason))
        // ... remaining branches ...
    }
}
```

---

### ARCH-07 — Firestore/GCP models used as domain entities

**Wrong:**
```kotlin
// services/GetProduct.kt
import com.google.cloud.firestore.DocumentSnapshot  // GCP in services!

fun execute(id: ProductId): DocumentSnapshot? =
    productRepository.findById(id)  // output port returns a Firestore type
```

**Correct:**
```kotlin
// domain/ports/output/ProductRepository.kt
interface ProductRepository {
    fun findById(id: ProductId): Product?  // returns a domain type
}

// adapters/output/firestore/FirestoreProductRepository.kt
override fun findById(id: ProductId): Product? =
    firestore.collection("products").document(id.value).get().get()
        .toObject(ProductDocument::class.java)?.toDomain()
```

---

### CORO-01 — kotlinx.coroutines types in domain port signatures

**Wrong:**
```kotlin
// domain/ports/output/ProductRepository.kt
import kotlinx.coroutines.flow.Flow  // kotlinx.coroutines type in domain!

interface ProductRepository {
    fun findAll(): Flow<Product>
}
```

**Correct:**
```kotlin
// domain/ports/output/ProductRepository.kt
interface ProductRepository {
    fun findAll(): List<Product>  // plain by default; suspend is allowed, Flow is not
}
```

If streaming is genuinely essential, define the Flow boundary in `services/` or in the adapter, never
in `domain/`.

---

## Http4k misuse

### HTTP-03 — Side effects inside lens extraction

**Wrong:**
```kotlin
val orderLens = Body.auto<CreateOrderRequest>()
    .map(
        { request ->
            auditLog.record(request)  // Side effect during lens extraction!
            request
        },
        { it }
    ).toLens()
```

**Correct:**
```kotlin
val orderLens = Body.auto<CreateOrderRequest>().toLens()

val handler: HttpHandler = { request ->
    val orderRequest = orderLens(request)
    auditLog.record(orderRequest)  // Side effect in handler
    // ...
}
```

---

### HTTP-01 — Raw string parsing from request

**Wrong:**
```kotlin
val handler: HttpHandler = { request ->
    val productId = request.query("productId")  // Raw string, no type safety
    // ...
}
```

**Correct:**
```kotlin
val productIdLens = Query.string().required("productId")

val handler: HttpHandler = { request ->
    val productId = ProductId(productIdLens(request))
    // ...
}
```

---

### HTTP-02 — Throwing exceptions from handlers

**Wrong:**
```kotlin
val handler: HttpHandler = { request ->
    val product = productRepository.findById(id)
        ?: throw NotFoundException("Product not found")  // Exception from handler!
}
```

**Correct:**
```kotlin
val handler: HttpHandler = { request ->
    when (val result = getProduct.execute(id)) {
        is GetProductResult.Success -> Response(Status.OK).with(productLens of result.product.toResponse())
        is GetProductResult.NotFound -> Response(Status.NOT_FOUND)
    }
}
```

---

## Error handling

### ERR-06 — `null` to signal failure

**Wrong:**
```kotlin
interface OrderRepository {
    fun save(order: Order): Order?  // null means failure?
}
```

**Correct:**
```kotlin
sealed interface SaveOrderResult {
    data class Success(val order: Order) : SaveOrderResult
    data object DuplicateOrder : SaveOrderResult
    data class StorageError(val cause: Throwable) : SaveOrderResult
}

interface OrderRepository {
    fun save(order: Order): SaveOrderResult
}
```

---

### ERR-07 — Non-exhaustive `when` on sealed types

**Wrong:**
```kotlin
val response = when (result) {
    is CreateOrderResult.Success -> Response(Status.CREATED)
    // ValidationFailed and ProductNotFound silently ignored — compiler warning suppressed
    else -> Response(Status.INTERNAL_SERVER_ERROR)
}
```

**Correct:**
```kotlin
val response = when (result) {
    is CreateOrderResult.Success -> Response(Status.CREATED).with(orderLens of result.order.toResponse())
    is CreateOrderResult.ValidationFailed -> Response(Status.BAD_REQUEST).with(errorLens of ErrorResponse(result.reason))
    is CreateOrderResult.ProductNotFound -> Response(Status.NOT_FOUND)
    CreateOrderResult.InsufficientStock -> Response(Status.CONFLICT)
    // All branches handled — compiler enforces this
}
```
