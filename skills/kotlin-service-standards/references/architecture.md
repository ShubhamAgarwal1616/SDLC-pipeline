# Architecture Reference — DDD + Hexagonal (Ports & Adapters)

## Package structure (treat as strict and enforceable)

```
src/
├── main/kotlin/com/example/<service>/
│   ├── domain/
│   │   ├── model/                 # Entities, value objects, aggregates, sealed result types
│   │   └── ports/
│   │       ├── input/             # Driving port interfaces (see "Input ports" below)
│   │       └── output/            # Driven port interfaces (repositories, gateways)
│   ├── services/                  # Application services — one business action per class, execute()
│   ├── adapters/
│   │   ├── input/
│   │   │   ├── http/              # Http4k routes, lenses, HTTP models + mappers (driving side)
│   │   │   └── pubsub/            # Event-driven consumers (driving side, depend on input ports)
│   │   └── output/
│   │       ├── firestore/         # Firestore adapter + document models/mappers
│   │       ├── pubsub/            # Pub/Sub publishers + message models/mappers
│   │       ├── storage/           # Cloud Storage adapter
│   │       └── cache/             # Caffeine caching decorators for output ports
│   ├── Dependencies.kt            # Manual DI — everything wired here
│   └── Application.kt             # Http4k server wiring + lifecycle (Closeable)
└── test/kotlin/com/example/<service>/
    ├── acceptance/                # Black-box tests of the fully wired application
    ├── services/                  # Service tests with fakes/mocks for output ports
    └── adapters/
        └── output/
            ├── firestore/         # Adapter integration / emulator tests
            └── cache/             # Cache decorator tests
```

### Key structural decisions

- **Adapters are split by direction:** `adapters/input/` (driving) and `adapters/output/` (driven).
  This names which side of the hexagon you are on — clearer than a single "infrastructure" bucket.
- **Pub/Sub appears on both sides:** consuming events is driving (`adapters/input/pubsub/`);
  publishing events is driven (`adapters/output/pubsub/`).
- **Sealed result/error types live in `domain/model/`**, next to the entities they describe.

---

## Layer responsibilities

### `domain/`
The heart of the application. Pure business concepts, no framework dependencies.

- **`domain/model/`** — Entities, value objects, aggregates, and the sealed result types returned by
  services. Pure data + business invariants.
- **`domain/ports/output/`** — Interfaces describing what the application needs *from* the outside.
  Named `SomethingRepository` (data) or `SomethingGateway` (external system).
- **`domain/ports/input/`** — Driving port interfaces. See the dedicated section below — these are
  conditional, not always present.

### `services/`
Application services. **One class per business action**, named as a **verb phrase**
(`CreateOrder`, `GetProduct`, `CancelSubscription`), exposing a single `execute(...)`.
A service orchestrates domain logic through output ports and returns a sealed result type.

```kotlin
// services/CreateOrder.kt
class CreateOrder(
    private val productRepository: ProductRepository,
    private val orderRepository: OrderRepository
) {
    fun execute(command: CreateOrderCommand): CreateOrderResult {
        val product = productRepository.findById(command.productId)
            ?: return CreateOrderResult.ProductNotFound(command.productId)
        return orderRepository.save(Order.from(command, product)).let { saved ->
            CreateOrderResult.Success(saved)
        }
    }
}
```

A service called directly (e.g. by an HTTP route) is a plain class and does not implement an
interface. It implements an input port **only** when a driving adapter needs the abstraction
(see below).

### `adapters/`
Technical adapters, split by direction.

- **`adapters/input/http/`** — Http4k routes, lenses, HTTP DTOs and their mappers.
- **`adapters/input/pubsub/`** — Event-driven consumers. These depend on **input ports**.
- **`adapters/output/firestore` | `pubsub` | `storage`** — driven adapters implementing output ports.
- **`adapters/output/cache/`** — Caffeine caching decorators around output ports.

### `Application.kt` and `Dependencies.kt`
The composition root. The **only** place where all layers may meet.

---

## Input ports — when to define them

Input (driving) ports are interfaces in `domain/ports/input/` that a service implements, so a
driving adapter depends on an abstraction rather than a concrete service.

| Driving adapter | Input port needed? | Why |
|---|---|---|
| HTTP (Http4k route) | **No** | The route calls the concrete service directly. Adding an interface here is indirection with no payoff. |
| Event-driven consumer (Pub/Sub) | **Yes** | The consumer should be decoupled from the service implementation and testable in isolation against the port. |

**HTTP case (no input port):**

```kotlin
// services/GetProduct.kt — concrete, called directly by the route
class GetProduct(private val productRepository: ProductRepository) {
    fun execute(id: ProductId): GetProductResult =
        productRepository.findById(id)
            ?.let { GetProductResult.Success(it) }
            ?: GetProductResult.NotFound(id)
}
```

**Event-driven case (input port):**

```kotlin
// domain/ports/input/ProcessOrder.kt
interface ProcessOrder {
    fun execute(command: ProcessOrderCommand): ProcessOrderResult
}

// services/ProcessOrderService.kt
class ProcessOrderService(
    private val orderRepository: OrderRepository
) : ProcessOrder {
    override fun execute(command: ProcessOrderCommand): ProcessOrderResult = /* ... */
}

// adapters/input/pubsub/OrderEventConsumer.kt — depends on the port, not the service
class OrderEventConsumer(private val processOrder: ProcessOrder) { /* ... */ }
```

**Naming:** the input port takes the verb-phrase name (`ProcessOrder`); its implementation gets a
`Service` suffix (`ProcessOrderService`). A service with no input port keeps the plain verb-phrase
name (`GetProduct`).

---

## Dependency rules (all violations are errors)

| Layer | May import | Must not import |
|---|---|---|
| `domain/` | stdlib, pure Kotlin | GCP, Http4k, Jackson, kotlinx.coroutines types |
| `services/` | `domain/` only | adapters directly; GCP, Http4k, Jackson |
| `adapters/input/` | `domain/` (incl. input ports), `services/`, Http4k, Jackson, Config4k | — |
| `adapters/output/` | `domain/` (output ports), GCP, Jackson, Config4k, Caffeine | `services/`, Http4k |
| `Application.kt` / `Dependencies.kt` | all layers | — |

Output adapters never depend on services or on the HTTP layer.

---

## Concurrency stance — read this before adding `suspend`

**Plain, synchronous functions are the default.** Do **not** make ports, services, or adapters
`suspend` reflexively. The `CreateOrder` and `GetProduct` examples above are synchronous on purpose.

Introduce `suspend` / coroutines **only when there is a concrete benefit**:

- fan-out concurrency — several independent IO calls that should run in parallel,
- non-blocking IO under high request concurrency,
- streaming / backpressure.

When you *do* reach for coroutines, apply these rules (and only then):

- `suspend` is a language keyword (no import) and **may** appear on ports. kotlinx.coroutines
  **types** — `Flow`, `Deferred`, `Channel`, `CoroutineScope`, `Job`, dispatchers — must **never**
  appear in `domain/`, including port signatures.
- Wrap blocking SDK calls in `withContext(Dispatchers.IO)`.
- Use structured concurrency: a lifecycle-bound `CoroutineScope(SupervisorJob() + ...)`, never
  `GlobalScope`.
- `runBlocking` is allowed **only** at a non-coroutine SDK callback boundary (e.g. a Pub/Sub
  `MessageReceiver`), never in services, domain, or HTTP handlers.

**Why:** a default-synchronous service is simpler to read, test, and reason about. Coroutines are a
tool for specific problems (concurrency, scale, streaming), not a baseline tax on every signature.
Making everything `suspend` forces a coroutines runtime into tests that do not need one and hides
where real asynchrony actually happens.

---

## Caching rule

Caffeine caches live in `adapters/output/cache/` as **decorators** implementing output port
interfaces.

```kotlin
// adapters/output/cache/CachingProductRepository.kt
class CachingProductRepository(
    private val delegate: ProductRepository,
    private val cache: Cache<ProductId, Product>
) : ProductRepository {

    override fun findById(id: ProductId): Product? =
        cache.getIfPresent(id) ?: delegate.findById(id)?.also { cache.put(id, it) }
}
```

Wired in `Dependencies.kt`:

```kotlin
val productRepository: ProductRepository = CachingProductRepository(
    delegate = FirestoreProductRepository(firestore),
    cache = Caffeine.newBuilder().maximumSize(1000).expireAfterWrite(5, TimeUnit.MINUTES).build()
)
```

Caching never appears in domain or service code.

---

## Mapping rule

Mappers between adapter models (Firestore documents, HTTP DTOs, Pub/Sub payloads) and domain types
live in the **adapter's own package**, as extension functions:

```kotlin
// adapters/output/firestore/ProductDocument.kt
fun ProductDocument.toDomain(): Product = Product(id = ProductId(this.id), name = this.name)
fun Product.toDocument(): ProductDocument = ProductDocument(id = this.id.value, name = this.name)
```

Mapping code **never** lives in `domain/` or `services/`.

**Why:** mapping is an adapter concern. If the Firestore schema changes, only the adapter and its
mapper change — the domain is untouched.

---

## Naming conventions

| Concept | Convention | Example |
|---|---|---|
| Application service (no input port) | verb-phrase class + `execute()` | `CreateOrder`, `GetProduct` |
| Input port (event-driven) | verb-phrase interface + `execute()` | `interface ProcessOrder` |
| Input port implementation | `*Service` suffix | `ProcessOrderService` |
| Output port (data) | `interface SomethingRepository` | `ProductRepository` |
| Output port (external) | `interface SomethingGateway` | `PaymentGateway` |
| Firestore adapter | technology prefix | `FirestoreProductRepository` |
| Pub/Sub adapter | technology prefix | `PubSubEventGateway` |
| Cache decorator | `Caching` prefix | `CachingProductRepository` |
| HTTP request/response model | `*Request` / `*Response` suffix | `CreateOrderRequest`, `OrderResponse` |
| Sealed result type | `*Result` suffix | `CreateOrderResult` |

HTTP models live only in `adapters/input/http/` and are **never** passed into services or domain.
Map at the route handler boundary.

---

## Application wiring pattern

```kotlin
class Application(private val dependencies: Dependencies) : Closeable {

    private val mainHandler: HttpHandler =
        ServerFilters.OpenTelemetryTracing(dependencies.openTelemetry)
            .then(/* other filters */)
            .then(ApiHandler(dependencies))

    private val server: Http4kServer =
        GracefulShutdownHttp4kServer(mainHandler.asK8sServer(...), ::onShutdown)

    fun start(): Application {
        server.start()
        return this
    }

    private fun onShutdown() {
        // stop subscribers, flush, etc.
    }

    override fun close() { server.stop() }
}

fun main() {
    val dependencies = Dependencies(ProductionConfiguration().extractConfig())
    Application(dependencies).start()
}
```

If — and only if — the service runs background coroutines (e.g. a Pub/Sub subscriber), introduce a
lifecycle-bound scope at this composition root:

```kotlin
private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

fun start(): Application {
    server.start()
    scope.launch { dependencies.orderEventConsumer.start() }
    return this
}

private fun onShutdown() {
    scope.cancel()
    dependencies.orderEventConsumer.stop()
}
```

### Principles enforced by this pattern

- **No coroutines you don't need:** a request/response service with synchronous adapters needs no
  scope at all.
- **Structured concurrency:** if a scope exists, all coroutines are its children; cancelling it
  cancels them. Lifecycle-bound — never `GlobalScope`.
- **`SupervisorJob`:** a failure in one coroutine (e.g. a single message handler) does not cancel
  the whole scope.
- **Explicit wiring:** all dependencies are constructed in `Dependencies.kt`. No DI framework magic.

---

## Error modeling

Define sealed result types in `domain/model/`:

```kotlin
// domain/model/CreateOrderResult.kt
sealed interface CreateOrderResult {
    data class Success(val order: Order) : CreateOrderResult
    data class ValidationFailed(val reason: String) : CreateOrderResult
    data class ProductNotFound(val productId: ProductId) : CreateOrderResult
    data object InsufficientStock : CreateOrderResult
}
```

The service returns the result type; the route handler maps it to an HTTP response:

```kotlin
// adapters/input/http/OrderRoutes.kt
val response = when (val result = createOrder.execute(command)) {
    is CreateOrderResult.Success -> Response(Status.CREATED).with(orderLens of result.order.toResponse())
    is CreateOrderResult.ValidationFailed -> Response(Status.BAD_REQUEST).with(errorLens of ErrorResponse(result.reason))
    is CreateOrderResult.ProductNotFound -> Response(Status.NOT_FOUND)
    CreateOrderResult.InsufficientStock -> Response(Status.CONFLICT)
}
```

Arrow is **not** a project dependency. Do not use `Either`.
