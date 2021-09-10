package com.johnlewis.offercodepublisher.runner

import com.google.cloud.Timestamp
import com.google.cloud.datastore.Datastore
import com.google.cloud.datastore.Entity
import com.google.cloud.datastore.Key
import com.google.cloud.datastore.QueryResults
import org.junit.jupiter.api.Test
import com.google.cloud.datastore.*
import com.johnlewis.offercodepublisher.entities.OfferCode
import com.johnlewis.offercodepublisher.mapper.OfferCodeMapper
import com.johnlewis.offercodepublisher.services.impl.PubSubPublisherImpl
import io.mockk.*

class OfferPublisherRunnerTest {

    private val datastore = mockk<Datastore>()
    private val pubSubPublisher = mockk<PubSubPublisherImpl>()
    private val mapper = mockk<OfferCodeMapper>()
    private val offerPublisherRunner = OfferPublisherRunner(datastore, pubSubPublisher, mapper)
    private val entity = mockk<Entity>()
    private val queryResults = mockk<QueryResults<Entity>>()
    private val key = mockk<Key>()
    private val filter = mockk<StructuredQuery.Filter>()
    private val couponId = "coupon_id"
    private val name = "name"
    private val title = "title"
    private val promotionId = "promotionId"
    private val offerCode = OfferCode(
        couponId = couponId,
        promotionId = promotionId,
        name = name,
        title = title,
        startDate =  Timestamp.parseTimestamp("2021-04-01T15:25:22.455Z"),
        endDate = Timestamp.parseTimestamp("2022-06-01T05:08:12.988Z")
    )

    @Test
    fun `getEligibleOffersFromDatastore should return a list of report eligible offer codes`(){

        every { entity.key } returns key
        every { datastore.run(any<Query<Entity>>()) } returns queryResults
        every { queryResults.hasNext() } returns true andThen false
        every { queryResults.next() } returns entity
        every { mapper.createOfferCodeFrom(entity) } returns offerCode
        every { datastore.run(any<Query<Entity>>()) } returns queryResults

        offerPublisherRunner.getEligibleOffersFromDatastore()

        verify(exactly = 3) {
            datastore.run(any<Query<Entity>>())
        }
        verify(atLeast = 1) {
            queryResults.hasNext()
            queryResults.next()
            mapper.createOfferCodeFrom(any())
        }
    }

    @Test
    fun `performQuery method should return query results`(){

        every { datastore.run(any<Query<Entity>>()) } returns queryResults

        offerPublisherRunner.performQuery(filter)

        verify {
            datastore.run(any<Query<Entity>>())
        }
    }
}
