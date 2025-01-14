package com.johnlewis.offercodepublisher.configuration

import com.google.auth.oauth2.GoogleCredentials
import com.google.cloud.datastore.Datastore
import com.google.cloud.datastore.DatastoreOptions
import com.johnlewis.offercodepublisher.mapper.OfferCodeMapper
import com.johnlewis.offercodepublisher.runner.OfferPublisherRunner
import com.johnlewis.offercodepublisher.services.impl.PubSubPublisherImpl
import org.http4k.cloudnative.env.Environment
import org.http4k.cloudnative.env.MapEnvironment

object Dependencies {
    private val environment = Environment.fromOptionalResource("override.properties") overrides
        Environment.ENV overrides
        Environment.EMPTY

    private fun Environment.Companion.fromOptionalResource(resource: String): Environment {
        val stream = Environment::class.java.getResourceAsStream("/${resource.removePrefix("/")}")
        return if (stream == null) EMPTY else MapEnvironment.from(stream.reader())
    }

    private val datastore = initialiseDatastore(environment)

    private fun initialiseDatastore(environment: Environment): Datastore {
        val datastoreOptions = DatastoreOptions.getDefaultInstance().toBuilder()
            .setNamespace(Configuration.databaseNamespace(environment))
            .setProjectId(Configuration.projectId(environment))
            .build()
        return datastoreOptions.service
    }

    val offerPublisherRunner by lazy {
        OfferPublisherRunner(datastore, pubSubPublisher, offerCodeMapper)
    }

    val pubSubPublisher by lazy {
        PubSubPublisherImpl(Configuration.pubsubTopicId(environment), Configuration.source(environment))
    }
    val offerCodeMapper by lazy {
        OfferCodeMapper()
    }
}
