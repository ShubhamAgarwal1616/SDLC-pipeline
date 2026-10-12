package com.example.notes

import com.example.notes.adapters.input.http.ApiKeys
import com.example.notes.adapters.input.http.noteRoutes
import com.example.notes.adapters.output.ids.UuidIdGenerator
import com.example.notes.adapters.output.memory.InMemoryNoteRepository
import com.example.notes.domain.ports.output.IdGenerator
import com.example.notes.domain.ports.output.NoteRepository
import com.example.notes.services.CreateNote
import com.example.notes.services.ListNotes
import org.http4k.core.HttpHandler

/** The only place where all layers meet (with Application.kt). */
class Dependencies(
    apiKeys: ApiKeys,
    notes: NoteRepository = InMemoryNoteRepository(),
    ids: IdGenerator = UuidIdGenerator(),
) {
    val app: HttpHandler = noteRoutes(CreateNote(notes, ids), ListNotes(notes), apiKeys)
}
