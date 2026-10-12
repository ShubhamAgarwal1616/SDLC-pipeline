package com.example.notes.adapters.output.ids

import com.example.notes.domain.model.NoteId
import com.example.notes.domain.ports.output.IdGenerator
import java.util.UUID

class UuidIdGenerator : IdGenerator {
    override fun next(): NoteId = NoteId(UUID.randomUUID().toString())
}
