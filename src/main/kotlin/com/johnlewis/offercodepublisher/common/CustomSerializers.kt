package com.johnlewis.offercodepublisher.common

import com.fasterxml.jackson.core.JsonGenerator
import com.fasterxml.jackson.core.JsonProcessingException
import com.fasterxml.jackson.databind.JsonSerializer
import com.fasterxml.jackson.databind.SerializerProvider
import com.google.cloud.Timestamp
import java.io.IOException

class TimestampSerializer : JsonSerializer<Timestamp>() {

    @Throws(IOException::class, JsonProcessingException::class)
    override fun serialize(value: Timestamp?, gen: JsonGenerator?, serializers: SerializerProvider?) {
        gen?.let { jGen ->
            value?.let { timestamp ->
                jGen.writeString(timestamp.toString())
            } ?: jGen.writeNull()
        }
    }
}
