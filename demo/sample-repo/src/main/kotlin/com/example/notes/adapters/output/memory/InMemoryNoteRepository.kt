package com.example.notes.adapters.output.memory

import com.example.notes.domain.model.Note
import com.example.notes.domain.ports.output.NoteRepository
import java.util.concurrent.CopyOnWriteArrayList

class InMemoryNoteRepository : NoteRepository {
    private val notes = CopyOnWriteArrayList<Note>()

    override fun save(note: Note): Note = note.also { notes.add(it) }

    override fun all(): List<Note> = notes.toList()
}
