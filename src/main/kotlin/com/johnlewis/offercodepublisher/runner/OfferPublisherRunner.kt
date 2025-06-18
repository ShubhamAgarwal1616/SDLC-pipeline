package com.johnlewis.offercodepublisher.runner

import com.google.cloud.Timestamp
import com.google.cloud.datastore.*
import com.johnlewis.offercodepublisher.entities.OfferCode
import com.johnlewis.offercodepublisher.mapper.OfferCodeMapper
import com.johnlewis.offercodepublisher.services.PubSubPublisher
import mu.KotlinLogging
import java.time.LocalDateTime


class OfferPublisherRunner(
    private val datastore: Datastore,
    private val pubSubPublisher: PubSubPublisher,
    private val mapper: OfferCodeMapper
) {

    private val logger = KotlinLogging.logger {}

    fun offerPublisher() {

        pubSubPublisher.publishMessage(getEligibleOffersFromDatastore())
    }

    fun getEligibleOffersFromDatastore(): List<OfferCode> {

        val offerCodes = mutableListOf<OfferCode>()
        val twoYearsAgo = java.sql.Timestamp.valueOf(LocalDateTime.now().minusYears(2))

        logger.info {"Creating filters for query for time $twoYearsAgo"}
        val filters = listOf(
            StructuredQuery.CompositeFilter.and(
                StructuredQuery.PropertyFilter.isNull(
                    Constants.START_DATE
                )
            ),
            StructuredQuery.CompositeFilter.and(
                StructuredQuery.PropertyFilter.isNull(Constants.START_DATE),
                StructuredQuery.PropertyFilter.isNull(Constants.END_DATE)
            ),
            StructuredQuery.CompositeFilter.and(
                StructuredQuery.PropertyFilter.gt(
                    Constants.START_DATE,
                    Timestamp.of(java.sql.Timestamp.valueOf(LocalDateTime.now().minusYears(2)))
                )
            )
        )

        logger.info {"Perform query for each filter"}
        filters.forEach { filter ->
            performQuery(
                filter
            ).forEach {
                offerCodes.add(mapper.createOfferCodeFrom(it))
            }
        }

        logger.info {"Returning filtered offerCodes"}
        return offerCodes.distinctBy { offerCode ->
            offerCode.couponId
        }.filter {
            it.startDate != null && it.startDate > Timestamp.of(twoYearsAgo)
                || it.startDate == null && it.endDate == null
                || it.startDate == null && it.endDate != null && it.endDate > Timestamp.of(twoYearsAgo)
        }
    }

    fun performQuery(filter: StructuredQuery.Filter): QueryResults<Entity> {

        try {

            val query = Query
                .newEntityQueryBuilder()
                .setKind(Constants.OFFER_CODE_KIND)
                .setFilter(filter)
                .build()
            logger.info {"Successfully performed query, returning offerCodes "}
            return datastore.run(query)
        } catch (e: Exception) {
            logger.error(e) { "Exception thrown while getting records from ${Constants.OFFER_CODE_KIND} because ${e.cause} and with message ${e.message}" }
            throw e
        }
    }
}
