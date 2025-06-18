package com.johnlewis.offercodepublisher

import com.fasterxml.jackson.module.kotlin.jacksonObjectMapper
import com.google.auth.oauth2.ServiceAccountCredentials
import com.google.cloud.datastore.DatastoreOptions
import com.johnlewis.offercodepublisher.mapper.OfferCodeMapper
import com.johnlewis.offercodepublisher.runner.OfferPublisherRunner
import com.johnlewis.offercodepublisher.services.impl.PubSubPublisherImpl
import io.mockk.mockk
import org.junit.jupiter.api.Test
import java.nio.file.Files
import kotlin.io.path.fileSize
import kotlin.io.path.pathString
import kotlin.io.path.writeText

class ReportTest {

    private val pubSubPublisher = mockk<PubSubPublisherImpl>()
    private val mapper = OfferCodeMapper()

    @Test
    fun test() {
        val datastore = DatastoreOptions
            .newBuilder()
            .setProjectId("jl-entitlements-flex")
            .setNamespace("entitlements-checker-integration")
            .setCredentials(ServiceAccountCredentials.getApplicationDefault())
            .build()
            .service

        val runner = OfferPublisherRunner(datastore, pubSubPublisher, mapper)
        val result = runner.getEligibleOffersFromDatastore()
        val file = Files.createTempFile("report", ".json")

        file.writeText(jacksonObjectMapper().writeValueAsString(result))
        println(file.pathString)
        println("${result.size}")
        println("${file.fileSize()}")


    }
}