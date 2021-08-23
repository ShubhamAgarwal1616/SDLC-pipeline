package com.johnlewis.offercodepublisher.entities

import com.fasterxml.jackson.databind.annotation.JsonSerialize
import com.google.auto.value.extension.serializable.SerializableAutoValue
import com.google.cloud.Timestamp
import com.johnlewis.offercodepublisher.common.TimestampSerializer
import java.io.Serializable

@SerializableAutoValue
data class OfferCode(
    val couponId: String,
    val promotionId: String,
    val name: String,
    val title: String,
    @JsonSerialize(using = TimestampSerializer::class)
    val startDate: Timestamp?,
    @JsonSerialize(using = TimestampSerializer::class)
    val endDate: Timestamp?
) : Serializable

@SerializableAutoValue
data class ReportData(
    val total: Int,
    val offerCodes: List<OfferCode>
) : Serializable
