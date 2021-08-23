package com.johnlewis.offercodepublisher.mapper

import com.google.cloud.Timestamp
import com.google.cloud.datastore.Entity
import com.google.cloud.datastore.Key
import com.johnlewis.offercodepublisher.common.OfferCodeProperties
import io.mockk.*
import org.junit.jupiter.api.Test
import strikt.api.expect
import strikt.assertions.isEqualTo

class OfferCodeMapperTest {

    private val offerCodeMapper = OfferCodeMapper()
    private val couponId = "couponId"
    private val name = "name"
    private val title = "title"
    private val promotionId = "promotionId"
    private val timestampStartDate = Timestamp.parseTimestamp("2021-06-01T00:00:00Z")
    private val timestampEndDate = Timestamp.parseTimestamp("2022-06-01T00:00:00Z")

    @Test
    fun `createOfferCodeFrom should return OfferCode`() {

        val entity = mockk<Entity>()
        val key = mockk<Key>()
        mockkObject(Converters)

        every { entity.key } returns key
        every { key.name } returns couponId
        every { Converters.getAttributeAsString(entity, OfferCodeProperties.NAME) } returns name
        every { Converters.getAttributeAsString(entity, OfferCodeProperties.DESCRIPTION) } returns title
        every { Converters.getAttributeAsString(entity, OfferCodeProperties.CODE) } returns promotionId
        every { Converters.getAttributeAsNullableTimestamp(entity, OfferCodeProperties.START_DATE) } returns timestampStartDate
        every { Converters.getAttributeAsNullableTimestamp(entity, OfferCodeProperties.END_DATE) } returns timestampEndDate

        val expected = offerCodeMapper.createOfferCodeFrom(entity)

        verify(exactly = 1) {
            entity.key
            key.name
            Converters.getAttributeAsString(entity, OfferCodeProperties.NAME)
            Converters.getAttributeAsString(entity, OfferCodeProperties.DESCRIPTION)
            Converters.getAttributeAsString(entity, OfferCodeProperties.CODE)
            Converters.getAttributeAsNullableTimestamp(entity, OfferCodeProperties.START_DATE)
            Converters.getAttributeAsNullableTimestamp(entity, OfferCodeProperties.END_DATE)
        }

        expect {
            that(expected.promotionId) isEqualTo promotionId
            that(expected.title) isEqualTo  title
            that(expected.couponId) isEqualTo couponId
            that(expected.startDate) isEqualTo timestampStartDate
            that(expected.endDate) isEqualTo timestampEndDate
        }

        unmockkObject(Converters)
    }
}
