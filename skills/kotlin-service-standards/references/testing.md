# Testing Standards Reference

---

## Test structure mirrors main structure

```
test/kotlin/com/example/<service>/
├── acceptance/             # Black-box tests of the fully wired application
├── services/               # Service tests — MockK or hand-written fakes for output ports
└── adapters/
    └── output/
        ├── firestore/      # Emulator / integration tests per adapter
        └── cache/          # Cache decorator tests
```

TEST-01: `domain/` tests are pure unit tests — zero dependencies, no mocks needed.
TEST-02: `services/` tests use MockK (or hand-written fakes) for output ports.
TEST-03: `adapters/` tests are integration tests per adapter, testing wiring/mapping only.
TEST-04: `acceptance/` tests drive the fully wired app black-box, asserting behaviour end-to-end.

**Synchronous by default (CORO-00):** most code is plain, so tests need no coroutine scaffolding —
no `runTest`, no `runBlocking`. Only use `runTest` for code that is genuinely `suspend` (TEST-05).

---

## Kotest style

Use **DescribeSpec** or **ShouldSpec** as the default test style. Describe behaviour, not
implementation.

```kotlin
// services/CreateOrderTest.kt
class CreateOrderTest : DescribeSpec({

    val productRepository = mockk<ProductRepository>()
    val orderRepository = mockk<OrderRepository>()
    val createOrder = CreateOrder(productRepository, orderRepository)

    describe("execute") {
        context("when the product exists and has sufficient stock") {
            it("returns Success with the created order") {
                val product = aProduct(stockCount = 5)
                every { productRepository.findById(product.id) } returns product
                every { orderRepository.save(any()) } answers { SaveOrderResult.Success(firstArg()) }

                val result = createOrder.execute(CreateOrderCommand(CustomerId("cust-1"), listOf(OrderItem(product.id, 2))))

                result.shouldBeInstanceOf<CreateOrderResult.Success>()
                result.order.items shouldBe listOf(OrderItem(product.id, 2))
            }
        }

        context("when the product does not exist") {
            it("returns ProductNotFound") {
                every { productRepository.findById(any()) } returns null

                val result = createOrder.execute(CreateOrderCommand(CustomerId("cust-1"), listOf(OrderItem(ProductId("unknown"), 1))))

                result shouldBe CreateOrderResult.ProductNotFound(ProductId("unknown"))
            }
        }

        context("when stock is insufficient") {
            it("returns InsufficientStock") {
                val product = aProduct(stockCount = 1)
                every { productRepository.findById(product.id) } returns product

                val result = createOrder.execute(CreateOrderCommand(CustomerId("cust-1"), listOf(OrderItem(product.id, 5))))

                result shouldBe CreateOrderResult.InsufficientStock
            }
        }
    }
})
```

Note: synchronous services use plain `every { ... }`, not `coEvery`, and need no `runTest`.

### Assertions

- Use `shouldBe` for equality.
- Use `shouldBeInstanceOf<T>()` when checking a sealed subtype (it smart-casts the value).
- Use data class equality for domain objects — avoid field-by-field assertions.
- Assert sealed results by both type and value.

```kotlin
result shouldBe CreateOrderResult.ProductNotFound(ProductId("p-99"))

result.shouldBeInstanceOf<CreateOrderResult.Success>()
result.order.id shouldBe OrderId("ord-1")  // smart-cast after shouldBeInstanceOf
```

---

## Acceptance tests

`acceptance/` tests wire the real services and in-memory/fake adapters together and drive the app
through its front door (the Http4k handler), asserting observable behaviour. They are the primary
proof that a feature works end-to-end.

```kotlin
// acceptance/CreateOrderAcceptanceTest.kt
class CreateOrderAcceptanceTest : DescribeSpec({

    val productRepository = InMemoryProductRepository(listOf(aProduct(stockCount = 5)))
    val orderRepository = InMemoryOrderRepository()
    val handler = ApiHandlerForTest(productRepository, orderRepository)  // wires real routes + services

    describe("POST /api/v1/orders") {
        it("creates an order and returns 201") {
            val response = handler(
                Request(Method.POST, "/api/v1/orders")
                    .with(Body.auto<CreateOrderRequest>().toLens() of CreateOrderRequest("cust-1", listOf(OrderItemRequest("p-1", 2))))
            )

            response.status shouldBe Status.CREATED
            orderRepository.findAll().shouldHaveSize(1)
        }

        it("returns 404 when the product does not exist") {
            val response = handler(
                Request(Method.POST, "/api/v1/orders")
                    .with(Body.auto<CreateOrderRequest>().toLens() of CreateOrderRequest("cust-1", listOf(OrderItemRequest("missing", 1))))
            )

            response.status shouldBe Status.NOT_FOUND
        }
    }
})
```

Keep acceptance tests black-box: assert HTTP status/body and observable state through ports, not
internal calls.

---

## Test isolation — preloaded data (TEST-07)

Any test that relies on **preloaded data** — acceptance tests and integration/emulator tests against
a shared store (Firestore emulator, a database, a real bucket) — must be **self-contained**:

- **Seed** exactly the data the case needs **before** the test runs.
- **Remove** that data **after** the test runs, even if it fails.
- Never depend on data left behind by another test, and never assume an empty store.

This keeps cases independent and order-insensitive, so the suite is stable whether run whole,
filtered, or in parallel.

### Per-case seed/clean with Kotest lifecycle callbacks

Use `beforeEach`/`afterEach` (run around every leaf test). Put cleanup in `afterEach` so it runs even
when an assertion fails.

```kotlin
// adapters/output/firestore/FirestoreProductRepositoryTest.kt
class FirestoreProductRepositoryTest : DescribeSpec({

    val firestore = FirestoreOptions.newBuilder()
        .setEmulatorHost("localhost:8080")
        .setProjectId("test-project")
        .build()
        .service
    val repository = FirestoreProductRepository(firestore)

    val seeded = aProduct(id = ProductId("p-1"))

    beforeEach {
        firestore.collection("products").document(seeded.id.value).set(seeded.toDocument()).get()
    }
    afterEach {
        // Remove only what this test created — leave the store as we found it.
        firestore.collection("products").document(seeded.id.value).delete().get()
    }

    describe("findById") {
        it("returns the seeded product") {
            repository.findById(seeded.id) shouldBe seeded
        }
    }
})
```

### Unique keys per case

When cases run concurrently against the same store, isolate them with a unique key (or a per-test
collection/namespace) rather than a shared fixed id, so concurrent cases never collide:

```kotlin
describe("save") {
    it("round-trips a product") {
        val product = aProduct(id = ProductId("p-${UUID.randomUUID()}"))
        try {
            repository.save(product) shouldBe SaveProductResult.Success(product)
            repository.findById(product.id) shouldBe product
        } finally {
            firestore.collection("products").document(product.id.value).delete().get()
        }
    }
}
```

### Acceptance tests with a shared store

The same rule applies to acceptance tests when they run against a real store rather than fresh
in-memory fakes: seed the request's preconditions in `beforeEach` and tear them down in `afterEach`.
In-memory fakes constructed fresh per spec are naturally isolated, but if a fake is shared across
cases, clear it in `afterEach`:

```kotlin
val orderRepository = InMemoryOrderRepository()
afterEach { orderRepository.clear() }
```

---

## Never `Thread.sleep` in tests — wait on a condition (TEST-08)

A fixed sleep is both flaky (too short) and slow (too long). When a test must wait for something
asynchronous to settle — a published event to arrive, an eventually-consistent store to reflect a
write, a background handler to finish — assert the condition with Kotest's waiting primitives, which
poll until the assertion passes or a timeout elapses.

```kotlin
import io.kotest.assertions.nondeterministic.eventually
import kotlin.time.Duration.Companion.seconds

// WRONG — fixed sleep: flaky and slow
it("publishes an order-created event") {
    createOrder.execute(command)
    Thread.sleep(2000)                      // ❌ never
    fakeBroker.published.shouldHaveSize(1)
}

// CORRECT — poll the condition until it holds (or fails at the timeout)
it("publishes an order-created event") {
    createOrder.execute(command)
    eventually(5.seconds) {
        fakeBroker.published.shouldHaveSize(1)
    }
}
```

Related Kotest primitives:

- `eventually(duration) { ... }` — passes as soon as the block stops throwing; fails at the timeout.
- `continually(duration) { ... }` — asserts a condition stays true for the whole window.
- `until(duration) { predicate }` — waits for a boolean predicate.

For coroutine code that owns its own time, prefer **virtual time** (`runTest` + `advanceTimeBy` /
`advanceUntilIdle`) over wall-clock waiting — see below.

---

## Coroutine testing (only for `suspend` code)

TEST-05: if — and only if — the code under test is `suspend`, use `runTest` from
`kotlinx-coroutines-test`. Never `runBlocking` in tests. Synchronous code needs none of this.

```kotlin
class SuspendingProductRepositoryTest : DescribeSpec({

    describe("findById") {
        it("returns the product when it exists") {
            runTest {
                val repo = InMemorySuspendingProductRepository(listOf(aProduct()))
                repo.findById(ProductId("p-1")) shouldBe aProduct()
            }
        }
    }
})
```

### Controlling time

Use `StandardTestDispatcher` and `advanceUntilIdle()` to control virtual time:

```kotlin
it("retries after backoff") {
    runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        val retryService = RetryService(dispatcher)

        retryService.startRetry()
        advanceTimeBy(1100)  // advance past the 1-second delay
        advanceUntilIdle()

        retryService.attemptCount shouldBe 2
    }
}
```

### Flow / Turbine

Use Turbine for Flow assertions instead of `Thread.sleep()` (Flow only appears in `services/` or
adapters, never in `domain/` — CORO-01):

```kotlin
it("emits order events") {
    runTest {
        val eventGateway = FakeOrderEventGateway()

        eventGateway.events.test {
            // trigger the producing action
            awaitItem().shouldBeInstanceOf<OrderCreatedEvent>()
            cancelAndIgnoreRemainingEvents()
        }
    }
}
```

---

## Http4k handler testing

Test handlers directly as functions — do not spin up a real Jetty server for unit tests.

```kotlin
// adapters/input/http/ProductRoutesTest.kt
class ProductRoutesTest : DescribeSpec({

    val getProduct = mockk<GetProduct>()
    val createProduct = mockk<CreateProduct>()
    val handler = ProductRoutes(getProduct, createProduct).routes()

    describe("GET /products/{productId}") {
        context("when the product exists") {
            it("returns 200 with the product") {
                every { getProduct.execute(ProductId("p-1")) } returns GetProductResult.Success(aProduct())

                val response = handler(Request(Method.GET, "/products/p-1"))

                response.status shouldBe Status.OK
                Body.auto<ProductResponse>().toLens()(response).id shouldBe "p-1"
            }
        }

        context("when the product does not exist") {
            it("returns 404") {
                every { getProduct.execute(ProductId("p-99")) } returns GetProductResult.NotFound(ProductId("p-99"))

                val response = handler(Request(Method.GET, "/products/p-99"))

                response.status shouldBe Status.NOT_FOUND
            }
        }
    }

    describe("POST /products") {
        it("returns 400 when request body is invalid") {
            val response = handler(Request(Method.POST, "/products").body("not json"))
            response.status shouldBe Status.BAD_REQUEST
        }
    }
})
```

Use a fake `HttpHandler` for downstream HTTP dependencies:

```kotlin
val fakeDownstream: HttpHandler = { request ->
    when {
        request.uri.path.startsWith("/payments") -> Response(Status.OK).body("""{"status":"approved"}""")
        else -> Response(Status.NOT_FOUND)
    }
}
```

---

## GCP adapter testing

### Prefer fakes over mocks

Hand-written fakes are more robust than deeply mocked GCP SDK chains:

```kotlin
// test/.../services/InMemoryProductRepository.kt
class InMemoryProductRepository(
    initial: List<Product> = emptyList()
) : ProductRepository {
    private val store = initial.associateBy { it.id }.toMutableMap()

    override fun findById(id: ProductId): Product? = store[id]

    override fun save(product: Product): SaveProductResult {
        store[product.id] = product
        return SaveProductResult.Success(product)
    }

    override fun findAll(): List<Product> = store.values.toList()
}
```

### Emulator tests

When emulators are available, test the adapter against them directly:

```kotlin
// adapters/output/firestore/FirestoreProductRepositoryTest.kt
class FirestoreProductRepositoryTest : DescribeSpec({

    // Assumes Firestore emulator running on localhost:8080
    val firestore = FirestoreOptions.newBuilder()
        .setEmulatorHost("localhost:8080")
        .setProjectId("test-project")
        .build()
        .service

    val repository = FirestoreProductRepository(firestore)

    // Self-contained per TEST-07: each case cleans up what it wrote.
    val product = aProduct()
    afterEach { firestore.collection("products").document(product.id.value).delete().get() }

    describe("save and findById") {
        it("round-trips a product through Firestore") {
            repository.save(product) shouldBe SaveProductResult.Success(product)
            repository.findById(product.id) shouldBe product
        }
    }
})
```

When emulators are unavailable, mock the **port interface**, not GCP SDK internals:

```kotlin
// Use the fake in-memory implementation instead of mocking the Firestore SDK
val productRepository: ProductRepository = InMemoryProductRepository()
```

Never do this:

```kotlin
// WRONG — mocking GCP SDK internals is fragile
val mockApiFuture = mockk<ApiFuture<DocumentSnapshot>>()
val mockDocRef = mockk<DocumentReference>()
every { mockDocRef.get() } returns mockApiFuture
// ...
```

---

## Dependency wiring in tests

Wire dependencies manually in tests — do not use the production `Dependencies.kt`:

```kotlin
class CreateOrderTest : DescribeSpec({
    val productRepository = InMemoryProductRepository(listOf(aProduct()))
    val orderRepository = InMemoryOrderRepository()
    val createOrder = CreateOrder(productRepository, orderRepository)
    // ...
})
```

---

## Test builders / fixtures

Define test builders as top-level functions for reuse:

```kotlin
// test/.../Fixtures.kt
fun aProduct(
    id: ProductId = ProductId("p-1"),
    name: String = "Test Product",
    price: Money = Money(999L),
    stockCount: Int = 10
) = Product(id, name, price, stockCount)

fun anOrder(
    id: OrderId = OrderId("ord-1"),
    customerId: CustomerId = CustomerId("cust-1"),
    items: List<OrderItem> = listOf(OrderItem(ProductId("p-1"), 1)),
    status: OrderStatus = OrderStatus.PENDING
) = Order(id, customerId, items, status)
```

---

## Error path testing

Test error paths as first-class citizens. TEST-06: every sealed result branch must have a test.

```kotlin
describe("execute — error paths") {
    it("returns ProductNotFound when product does not exist") { /* ... */ }
    it("returns InsufficientStock when stock is too low") { /* ... */ }
    it("returns ValidationFailed when order has no items") { /* ... */ }
    // One test per branch in CreateOrderResult
}
```
