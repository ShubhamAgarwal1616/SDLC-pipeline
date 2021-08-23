package com.johnlewis.offercodepublisher.services

import com.johnlewis.offercodepublisher.entities.OfferCode

interface PubSubPublisher {

    fun publishMessage(offerCodes: List<OfferCode>)
}
