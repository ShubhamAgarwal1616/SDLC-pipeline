package com.johnlewis.offercodepublisher.common

import com.fasterxml.jackson.core.JsonGenerator
import com.fasterxml.jackson.core.JsonProcessingException
import com.fasterxml.jackson.databind.JsonSerializer
import com.fasterxml.jackson.databind.SerializerProvider
import com.google.cloud.Timestamp
import java.io.IOException
import java.time.format.DateTimeFormatter

class TimestampSerializer : JsonSerializer<Timestamp>() {

    var dateFormat = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'")

    @Throws(IOException::class, JsonProcessingException::class)
    override fun serialize(value: Timestamp?, gen: JsonGenerator?, serializers: SerializerProvider?) {

        gen?.let { jGen ->
            value?.let { timestamp ->
                jGen.writeString(dateFormat.format(timestamp.toSqlTimestamp().toLocalDateTime()))
            } ?: jGen.writeNull()
        }
    }
}
