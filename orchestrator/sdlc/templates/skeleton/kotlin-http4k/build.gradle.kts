plugins {
    kotlin("jvm") version "2.4.21"
    application
}

group = "{{group}}"
version = "0.1.0"

repositories {
    mavenCentral()
}

// The pipeline's sandbox builds offline and never adds dependencies: declare every library the
// service will need here, in a reviewed PR, before asking it to implement stories that use them.
dependencies {
    implementation(platform("org.http4k:http4k-bom:6.61.0.0"))
    implementation("org.http4k:http4k-core")
    implementation("org.http4k:http4k-format-jackson")

    testImplementation("io.kotest:kotest-runner-junit5:6.2.5")
    testImplementation("io.kotest:kotest-assertions-core:6.2.5")
    testImplementation("io.mockk:mockk:1.14.11")
}

kotlin {
    jvmToolchain(21)
}

application {
    mainClass.set("{{package}}.ApplicationKt")
}

tasks.test {
    useJUnitPlatform()
    testLogging {
        events("failed")
        exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.SHORT
    }
}
