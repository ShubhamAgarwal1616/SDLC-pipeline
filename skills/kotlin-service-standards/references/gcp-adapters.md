# GCP Adapter Patterns

GCP **driven** adapters live in `adapters/output/` and implement output ports from
`domain/ports/output/`. GCP **driving** adapters (event consumers) live in `adapters/input/`.
Map between SDK models and domain types at the adapter boundary (ARCH-07, ARCH-09).

**Synchronous by default (CORO-00).** The examples below are plain/blocking — appropriate for a
request/response service where the adapter runs on the request thread. Only switch an adapter to
`suspend` + `withContext(Dispatchers.IO)` when you have a concrete reason (fan-out concurrency,
non-blocking IO under high concurrency). The suspend variant is shown at the end.

---

## Firestore adapter (default: synchronous)

### Document model and mapper

```kotlin
// adapters/output/firestore/ProductDocument.kt
data class ProductDocument(
    val id: String = "",
    val name: String = "",
    val pricePence: Long = 0L,
    val stockCount: Int = 0
)

fun ProductDocument.toDomain() = Product(
    id = ProductId(id),
    name = name,
    price = Money(pricePence),
    stockCount = stockCount
)

fun Product.toDocument() = ProductDocument(
    id = id.value,
    name = name,
    pricePence = price.pence,
    stockCount = stockCount
)
```

### Repository implementation

```kotlin
// adapters/output/firestore/FirestoreProductRepository.kt
class FirestoreProductRepository(
    private val firestore: Firestore
) : ProductRepository {

    private val collection = "products"

    override fun findById(id: ProductId): Product? =
        firestore.collection(collection)
            .document(id.value)
            .get().get()
            .toObject(ProductDocument::class.java)
            ?.toDomain()

    override fun save(product: Product): SaveProductResult =
        runCatching {
            firestore.collection(collection)
                .document(product.id.value)
                .set(product.toDocument())
                .get()
            SaveProductResult.Success(product)
        }.getOrElse { cause ->
            // runCatching catches every Throwable. Rethrow CancellationException rather than
            // mapping it to a domain error — essential once this adapter becomes `suspend`
            // (CORO-00), where swallowing it would break structured concurrency.
            if (cause is CancellationException) throw cause
            SaveProductResult.StorageError(cause)
        }

    override fun findAll(): List<Product> =
        firestore.collection(collection)
            .get().get()
            .documents
            .mapNotNull { it.toObject(ProductDocument::class.java)?.toDomain() }
}
```

**Notes:**
- `.get().get()` is the blocking Firestore SDK pattern. On a synchronous adapter running on the
  request thread this is fine — do **not** add `withContext` unless the adapter is `suspend`.
- `kotlin.Result` via `runCatching` is acceptable inside the adapter as an implementation detail
  (ERR-03), but the returned type is always the sealed port result type. `runCatching` catches every
  `Throwable`, so rethrow `CancellationException` in the `getOrElse` (and let fatal JVM errors
  propagate) instead of mapping them to a `StorageError` — see the `save` example above.
- Never let Firestore exceptions propagate out of the adapter. Catch at the boundary and map to
  sealed error types (ERR-05).

---

## Pub/Sub publisher (output adapter)

### Message model and mapper

```kotlin
// adapters/output/pubsub/OrderEventMessage.kt
data class OrderEventMessage(
    val orderId: String,
    val eventType: String,
    val payload: String
)

fun OrderCreatedEvent.toMessage() = OrderEventMessage(
    orderId = orderId.value,
    eventType = "ORDER_CREATED",
    payload = objectMapper.writeValueAsString(this)
)
```

### Publisher

```kotlin
// adapters/output/pubsub/PubSubOrderEventGateway.kt
class PubSubOrderEventGateway(
    private val publisher: Publisher,
    private val objectMapper: ObjectMapper
) : OrderEventGateway {

    override fun publish(event: OrderCreatedEvent): PublishEventResult =
        runCatching {
            val message = event.toMessage()
            val data = ByteString.copyFromUtf8(objectMapper.writeValueAsString(message))
            val pubsubMessage = PubsubMessage.newBuilder().setData(data).build()
            publisher.publish(pubsubMessage).get()
            PublishEventResult.Success
        }.getOrElse { cause ->
            if (cause is CancellationException) throw cause
            PublishEventResult.PublishFailed(cause)
        }
}
```

---

## Pub/Sub consumer (input adapter) — uses an input port

A consumer is a **driving** adapter. Unlike an HTTP route, it depends on an **input port**
(`domain/ports/input/`) rather than a concrete service, so it is decoupled and testable in isolation
(ARCH-11).

```kotlin
// domain/ports/input/ProcessOrder.kt
interface ProcessOrder {
    fun execute(command: ProcessOrderCommand): ProcessOrderResult
}

// adapters/input/pubsub/OrderEventConsumer.kt
class OrderEventConsumer(
    private val subscriptionName: ProjectSubscriptionName,
    private val processOrder: ProcessOrder,
    private val objectMapper: ObjectMapper
) {
    private lateinit var subscriber: Subscriber

    fun start() {
        val receiver = MessageReceiver { message, consumer ->
            // The MessageReceiver callback runs on a Pub/Sub worker thread — not a coroutine, and not
            // a scarce event loop: blocking here is expected and bounded by flow control. It calls the
            // synchronous service directly; no runBlocking needed.
            val command = runCatching {
                objectMapper.readValue(message.data.toStringUtf8(), OrderEventMessage::class.java).toCommand()
            }.getOrElse {
                // Poison message: it can never deserialize, so redelivery won't help. Ack to drop it
                // (route to a dead-letter topic in a real system) rather than nack-looping forever.
                consumer.ack()
                return@MessageReceiver
            }
            // Act on the sealed result — an unhandled failure must never be silently acked (ERR-07, ERR-08).
            when (processOrder.execute(command)) {
                is ProcessOrderResult.Success -> consumer.ack()
                is ProcessOrderResult.Rejected -> consumer.ack()    // permanent failure → drop / dead-letter
                is ProcessOrderResult.Retryable -> consumer.nack()  // transient failure → let Pub/Sub redeliver
            }
        }
        subscriber = Subscriber.newBuilder(subscriptionName, receiver).build()
        subscriber.startAsync().awaitRunning()
    }

    fun stop() {
        if (::subscriber.isInitialized) subscriber.stopAsync()
    }
}
```

**Notes:**
- The consumer **acts on the sealed result**: ack on success or permanent rejection, `nack()` on a
  transient failure so Pub/Sub redelivers. Never ack a failure you have not handled — that silently
  drops the message (ERR-08).
- Deserialization is guarded at the boundary (ERR-08): a message that can never deserialize is a
  poison message — ack it or route it to a dead-letter topic, never let it nack-loop forever.
- With a synchronous service, the consumer calls `processOrder.execute(...)` directly. **No
  `runBlocking`.**
- `runBlocking` at the `MessageReceiver` boundary is the *only* accepted exception to CORO-04, and
  only if the service is `suspend`. Prefer keeping the service synchronous and avoiding the bridge.
- The consumer is started/stopped via the `Application.kt` lifecycle.

---

## Cloud Storage adapter

```kotlin
// adapters/output/storage/GcsFileGateway.kt
class GcsFileGateway(
    private val storage: Storage,
    private val bucketName: String
) : FileGateway {

    override fun upload(file: StoredFile): UploadFileResult =
        runCatching {
            val blobId = BlobId.of(bucketName, file.name)
            val blobInfo = BlobInfo.newBuilder(blobId)
                .setContentType(file.contentType)
                .build()
            storage.create(blobInfo, file.content)
            UploadFileResult.Success(file.name)
        }.getOrElse { cause ->
            if (cause is CancellationException) throw cause
            UploadFileResult.UploadFailed(cause)
        }

    override fun download(fileName: String): DownloadFileResult =
        runCatching {
            val blob = storage.get(BlobId.of(bucketName, fileName))
                ?: return@runCatching DownloadFileResult.NotFound(fileName)
            DownloadFileResult.Success(StoredFile(fileName, blob.getContent(), blob.contentType))
        }.getOrElse { cause ->
            if (cause is CancellationException) throw cause
            DownloadFileResult.DownloadFailed(cause)
        }
}
```

---

## When you do need non-blocking: the suspend variant

Only after deciding an adapter must be non-blocking (CORO-00), make the port `suspend` and wrap the
blocking SDK call:

```kotlin
// domain/ports/output/ProductRepository.kt
interface ProductRepository {
    suspend fun findById(id: ProductId): Product?
}

// adapters/output/firestore/FirestoreProductRepository.kt
override suspend fun findById(id: ProductId): Product? =
    withContext(Dispatchers.IO) {
        firestore.collection("products").document(id.value).get().get()
            .toObject(ProductDocument::class.java)?.toDomain()
    }
```

Everything downstream (service, route handler) then becomes `suspend` too — which is exactly why you
should not do this unless the concurrency benefit is real.

If a `suspend` adapter maps SDK failures to a sealed result, keep the `CancellationException`
rethrow shown in the synchronous examples above — under coroutines, swallowing it via `runCatching`
silently defeats structured concurrency.

---

## Dependencies wiring (GCP)

```kotlin
// Dependencies.kt
class Dependencies(private val config: AppConfig) {

    private val objectMapper = ObjectMapper().apply { findAndRegisterModules() }

    val firestore: Firestore = FirestoreOptions.getDefaultInstance().service
    val storage: Storage = StorageOptions.getDefaultInstance().service

    val productRepository: ProductRepository = CachingProductRepository(
        delegate = FirestoreProductRepository(firestore),
        cache = Caffeine.newBuilder().maximumSize(500).expireAfterWrite(10, TimeUnit.MINUTES).build()
    )

    val orderEventGateway: OrderEventGateway = PubSubOrderEventGateway(
        publisher = Publisher.newBuilder(TopicName.of(config.gcpProjectId, config.ordersTopic)).build(),
        objectMapper = objectMapper
    )

    val orderEventConsumer = OrderEventConsumer(
        subscriptionName = ProjectSubscriptionName.of(config.gcpProjectId, config.ordersSubscription),
        processOrder = ProcessOrderService(productRepository),
        objectMapper = objectMapper
    )
}
```
