package com.johnlewis.offercodepublisher

import com.fasterxml.jackson.module.kotlin.jacksonObjectMapper
import com.google.auth.oauth2.AccessToken
import com.google.auth.oauth2.GoogleCredentials
import com.google.auth.oauth2.ImpersonatedCredentials
import com.google.auth.oauth2.ServiceAccountCredentials
import com.google.cloud.datastore.DatastoreOptions
import com.johnlewis.offercodepublisher.mapper.OfferCodeMapper
import com.johnlewis.offercodepublisher.runner.OfferPublisherRunner
import com.johnlewis.offercodepublisher.services.impl.PubSubPublisherImpl
import io.mockk.mockk
import org.junit.jupiter.api.Test
import java.io.BufferedReader
import java.io.InputStreamReader
import java.nio.file.Files
import java.util.Date
import kotlin.io.path.fileSize
import kotlin.io.path.pathString
import kotlin.io.path.writeText

class ReportTest {

    private val pubSubPublisher = mockk<PubSubPublisherImpl>()
    private val mapper = OfferCodeMapper()

    fun getAccessToken(): String? {
        val process = ProcessBuilder("gcloud", "auth", "print-access-token")
            .redirectErrorStream(true)
            .start()

        BufferedReader(InputStreamReader(process.inputStream)).use { reader ->
            return reader.readLine()
        }
    }


     fun getToken(): ImpersonatedCredentials {
        // The email of the service account to impersonate
        val targetServiceAccountEmail = "put email here"
        val accessTokenString = requireNotNull(getAccessToken()) { "Failed to obtain gcloud access token" }
        // Create an AccessToken object
        val accessToken = AccessToken(accessTokenString, Date(System.currentTimeMillis() + 3600 * 1000))
        // Load user credentials using the access token
        val scopes = listOf("https://www.googleapis.com/auth/datastore")
        val userCredentials = GoogleCredentials.create(accessToken)
            .createScoped(scopes)
        // Create impersonated credentials
        val targetCredentials = ImpersonatedCredentials.create(
            userCredentials,
            targetServiceAccountEmail,
            emptyList(),
            scopes,
            3600
        )
        return targetCredentials
    }

    @Test
    fun test() {
        val datastore = DatastoreOptions
            .newBuilder()
            .setProjectId("jl-entitlements-flex")
            .setNamespace("entitlements-checker-integration")
            .setCredentials(getToken())
            .build()
            .service

        val runner = OfferPublisherRunner(datastore, pubSubPublisher, mapper)
        val result = runner.getEligibleOffersFromDatastore()
        val file = Files.createTempFile("report", ".json")

        file.writeText(jacksonObjectMapper().writeValueAsString(result))
        println("testing starts here")
        println(file.pathString)
        println("${result.size}")
        println("${file.fileSize()}")


    }
}