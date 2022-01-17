plugins {
    id("org.jetbrains.kotlin.jvm") version "1.4.20"
    id("org.owasp.dependencycheck") version "6.1.5"
    id("com.google.cloud.tools.jib") version "2.8.0"
    jacoco
}

val http4kVersion = "4.7.0.2"
val junitVersion = "5.7.1"
val mockkVersion = "1.11.0"
val striktVersion = "0.30.1"
val junitPlatformVersion = "1.7.1"
val slf4jVersion = "2.0.0-alpha1"
val logbackVersion = "1.3.0-alpha5"
val contrastVersion = "3.8.3.19749"

repositories {
    mavenCentral()
}

dependencies {

    implementation("com.google.cloud:google-cloud-datastore")
    implementation("com.google.cloud:google-cloud-pubsub")
    implementation(platform("com.google.cloud:libraries-bom:20.1.0"))
    implementation("org.slf4j:slf4j-api:$slf4jVersion")
    implementation("org.slf4j:jul-to-slf4j:$slf4jVersion")
    implementation("ch.qos.logback:logback-core:$logbackVersion")
    implementation("ch.qos.logback:logback-classic:$logbackVersion")
    implementation(platform("org.jetbrains.kotlin:kotlin-bom"))
    implementation("org.jetbrains.kotlin:kotlin-stdlib-jdk8")
    implementation("com.google.guava:guava:29.0-jre")
    implementation("org.owasp", "dependency-check-gradle", "6.1.5")
    implementation("io.github.microutils:kotlin-logging:1.7.8")
    implementation("ch.qos.logback:logback-classic:1.2.3")
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
    implementation("commons-codec", "commons-codec", "1.5")
    implementation("com.michael-bull.kotlin-result:kotlin-result:1.1.12")
    implementation("org.jetbrains.kotlin:kotlin-reflect:1.5.10")

    testImplementation("org.jetbrains.kotlin:kotlin-test")
    testImplementation("org.jetbrains.kotlin:kotlin-test-junit")
    testImplementation(platform("org.junit:junit-bom:$junitVersion"))
    testImplementation("org.junit.jupiter:junit-jupiter-api")
    testImplementation("org.junit.jupiter:junit-jupiter-params")
    testImplementation("org.junit.platform:junit-platform-launcher:$junitPlatformVersion")
    testImplementation("io.mockk:mockk:$mockkVersion")
    testImplementation("io.strikt:strikt-core:$striktVersion")

    testRuntimeOnly("org.junit.jupiter:junit-jupiter-engine")

    runtimeOnly("com.contrastsecurity:contrast-agent:$contrastVersion")
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
        jvmTarget = "11"
        languageVersion = "1.4"
        apiVersion = "1.4"
    }
}

jacoco {
    toolVersion = "0.8.6"
    reportsDir = file("$buildDir/customJacocoReportDir")
}

buildscript {
    dependencies {
        classpath("com.google.guava:guava") {
            version {
                strictly("30.1.1-jre")
            }
        }
        classpath("com.google.cloud.tools:jib-layer-filter-extension-gradle:0.1.0")
    }
}

jib {
    container {
        mainClass = "com.johnlewis.offercodepublisher.ApplicationKt"
        creationTime = "USE_CURRENT_TIMESTAMP"
        jvmFlags = listOf("-Dcontrast.dir=/tmp/contrast", "-javaagent:/app/libs/contrast-agent-$contrastVersion.jar")
    }

    from {
        image = "eu.gcr.io/jl-container-images/jldp-security/distroless-jvm:11"
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
