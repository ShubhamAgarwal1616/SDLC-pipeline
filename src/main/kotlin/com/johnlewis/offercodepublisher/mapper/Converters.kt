package com.johnlewis.offercodepublisher.mapper

import com.google.cloud.Timestamp
import com.google.cloud.datastore.Entity

object Converters {

    fun getAttributeAsString(entity: Entity, attribute: String): String {
        return entity.properties[attribute]?.get().toString()
    }

    fun getAttributeAsTimestamp(entity: Entity, attribute: String): Timestamp {
        return entity.getTimestamp(attribute)
    }

    fun getAttributeAsNullableTimestamp(entity: Entity, attribute: String): Timestamp? {
        return if (entity.isNull(attribute)) null else getAttributeAsTimestamp(entity, attribute)
    }
}
