package com.johnlewis.offercodepublisher.services.impl

import com.fasterxml.jackson.module.kotlin.jacksonObjectMapper
import com.google.cloud.Timestamp
import com.google.cloud.pubsub.v1.Publisher
import com.google.protobuf.ByteString
import com.google.pubsub.v1.PubsubMessage
import com.johnlewis.offercodepublisher.entities.OfferCode
import com.johnlewis.offercodepublisher.entities.ReportData
import com.johnlewis.offercodepublisher.services.PubSubPublisher
import mu.KotlinLogging
import java.util.concurrent.TimeUnit

class PubSubPublisherImpl(
    private val topicId: String,
    private val source: String
) : PubSubPublisher {

    private val logger = KotlinLogging.logger {}

    override fun publishMessage(offerCodes: List<OfferCode>) {

        var publisher: Publisher? = null
        try {
            publisher = Publisher.newBuilder(topicId).build()
            val mapper = jacksonObjectMapper()

            val reportData = ReportData(
                total = offerCodes.size,
                offerCodes = offerCodes
            )

            val pubsubMessage = PubsubMessage.newBuilder()
                .setData(ByteString.copyFrom(mapper.writeValueAsString(reportData), Charsets.UTF_8))
                .putAllAttributes(
                    mapOf(
                        "source" to source,
                        "specversion" to "1.0",
                        "datacontenttype" to "application/json",
                        "time" to Timestamp.now().toString(),
                        "subject" to "offer-code-report-" + Timestamp.now()
                    )
                )
                .build()

            val messageIdFuture = publisher.publish(pubsubMessage)
            messageIdFuture.get()

            logger.info { "Published ${reportData.total} offer codes." }
        } catch (e: Exception) {
            throw RuntimeException("Could not publish message  ${e.message}")
        } finally {
            if (publisher != null) {

                publisher.shutdown()
                publisher.awaitTermination(1, TimeUnit.MINUTES)
            }
        }
    }
}
