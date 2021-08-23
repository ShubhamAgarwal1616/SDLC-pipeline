package com.johnlewis.offercodepublisher.configuration

import org.http4k.cloudnative.env.EnvironmentKey
import org.http4k.lens.string

object Configuration {
    val databaseNamespace = EnvironmentKey.string().required("DATASTORE_NAMESPACE")
    val projectId = EnvironmentKey.string().required("PROJECT_ID")
    val pubsubTopicId = EnvironmentKey.string().required("TOPIC_ID")
    val source = EnvironmentKey.string().required("SOURCE")
}
