plugins {
    kotlin("jvm") version "1.9.0"
    id("org.owasp.dependencycheck") version "8.1.1"
    id("com.google.cloud.tools.jib") version "2.8.0"
    jacoco
}

val http4kVersion = "5.45.2.0"
val junitVersion = "5.12.2"

val mockkVersion = "1.14.0"
val striktVersion = "0.35.1"
val junitPlatformVersion = "1.12.2"
val slf4jVersion = "2.0.17"
val logbackVersion = "1.5.18"
val kotlinVersion = "1.7.21"


repositories {
    mavenCentral()
}

dependencies {
    implementation("com.google.cloud:google-cloud-datastore")
    implementation("com.google.cloud:google-cloud-pubsub")

    implementation(platform("com.google.cloud:libraries-bom:26.59.0"))
    implementation("org.slf4j:slf4j-api:$slf4jVersion")
    implementation("org.slf4j:jul-to-slf4j:$slf4jVersion")
    implementation("ch.qos.logback:logback-core:$logbackVersion")
    implementation("ch.qos.logback:logback-classic:$logbackVersion")
    implementation(platform("org.jetbrains.kotlin:kotlin-bom:$kotlinVersion"))
    implementation(kotlin("stdlib-jdk8"))
    implementation("com.google.guava:guava:32.0.0")
    implementation("io.github.microutils:kotlin-logging:1.7.8")
    implementation("net.logstash.logback:logstash-logback-encoder:6.3")
    implementation(platform("org.http4k:http4k-bom:$http4kVersion"))
    implementation("org.http4k:http4k-core")
    implementation("org.http4k:http4k-client-okhttp")
    implementation("org.http4k:http4k-server-jetty") {
        exclude(group = "org.eclipse.jetty.websocket")
    }
    implementation("org.http4k:http4k-format-jackson")
    implementation("org.http4k:http4k-client-jetty")
    implementation("org.http4k:http4k-cloudnative")
    implementation("org.http4k:http4k-metrics-micrometer")
    implementation("io.github.resilience4j", "resilience4j-circuitbreaker", "1.7.0")
    implementation("commons-codec", "commons-codec", "1.18.0")
    implementation("com.michael-bull.kotlin-result:kotlin-result:1.1.12")
    implementation("org.jetbrains.kotlin:kotlin-reflect:$kotlinVersion")

    testImplementation("org.jetbrains.kotlin:kotlin-test:$kotlinVersion")
    testImplementation("org.jetbrains.kotlin:kotlin-test-junit5:$kotlinVersion")
    testImplementation(platform("org.junit:junit-bom:$junitVersion"))
    testImplementation("org.junit.jupiter:junit-jupiter-api")
    testImplementation("org.junit.jupiter:junit-jupiter-params")
    testImplementation("org.junit.platform:junit-platform-launcher:$junitPlatformVersion")
    testImplementation("io.mockk:mockk:$mockkVersion")
    testImplementation("io.strikt:strikt-core:$striktVersion")

    testRuntimeOnly("org.junit.jupiter:junit-jupiter-engine")

}

tasks.test {
    useJUnitPlatform()
    testLogging {
        events("FAILED", "SKIPPED")
        exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL
    }
    finalizedBy(tasks.jacocoTestReport)
}

tasks.jacocoTestReport {
    reports {
        xml.isEnabled = true
        csv.isEnabled = false
        html.destination = file("${buildDir}/jacocoHtml")
        xml.destination = file("${buildDir}/jacoco/jacoco.xml")
    }

    dependsOn(tasks.test) // tests are required to run before generating the report
}

tasks.withType<org.jetbrains.kotlin.gradle.tasks.KotlinCompile> {
    kotlinOptions {
        jvmTarget = "17"
    }
}

jacoco {
    toolVersion = "0.8.13"
    reportsDir = file("$buildDir/customJacocoReportDir")
}

buildscript {
    dependencies {
        classpath("com.google.guava:guava") {
            version {
                strictly("30.1.1-jre")
            }
        }
        classpath("com.google.cloud.tools:jib-layer-filter-extension-gradle:0.3.0")
    }
}

jib {
    container {
        mainClass = "com.johnlewis.offercodepublisher.ApplicationKt"
        creationTime = "USE_CURRENT_TIMESTAMP"
    }

    from {
        image = "eu.gcr.io/jl-container-images/jldp-security/distroless-java17:latest"
    }
    to {
        image = "eu.gcr.io/jl-container-images/entitlements/offer-code-publisher"
        tags = listOfNotNull("latest", System.getenv("CI_PIPELINE_IID")).toSortedSet()
    }

    pluginExtensions {
        pluginExtension {
            implementation = "com.google.cloud.tools.jib.gradle.extension.layerfilter.JibLayerFilterExtension"
            @Suppress("RedundantSamConstructor")
            configuration(Action<com.google.cloud.tools.jib.gradle.extension.layerfilter.Configuration> {
                filters {
                    filter {
                        glob = "**/override.properties"
                    }
                }
            })
        }
    }
}
