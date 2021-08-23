package com.johnlewis.offercodepublisher.mapper

import com.google.cloud.datastore.Entity
import com.johnlewis.offercodepublisher.common.OfferCodeProperties
import com.johnlewis.offercodepublisher.entities.OfferCode

class OfferCodeMapper {

    fun createOfferCodeFrom(entity: Entity): OfferCode {

        return OfferCode(
            couponId = entity.key.name,
            name = Converters.getAttributeAsString(entity, OfferCodeProperties.NAME),
            title = Converters.getAttributeAsString(entity, OfferCodeProperties.DESCRIPTION),
            promotionId = Converters.getAttributeAsString(entity, OfferCodeProperties.CODE),
            startDate = Converters.getAttributeAsNullableTimestamp(entity, OfferCodeProperties.START_DATE),
            endDate = Converters.getAttributeAsNullableTimestamp(entity, OfferCodeProperties.END_DATE)
        )
    }
}
