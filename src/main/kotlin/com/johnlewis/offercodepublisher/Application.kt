package com.johnlewis.offercodepublisher

import com.johnlewis.offercodepublisher.configuration.Dependencies
import com.johnlewis.offercodepublisher.runner.OfferPublisherRunner
import mu.KotlinLogging
import kotlin.system.exitProcess

fun main() {

    val logger = KotlinLogging.logger {}

    try {
        run(Dependencies.offerPublisherRunner)
    } catch (e: Exception) {
        logger.error(e) { "Startup failed" }
        exitProcess(1)
    }
}

fun run(cleanupRunner: OfferPublisherRunner) {

    cleanupRunner.offerPublisher()
}
