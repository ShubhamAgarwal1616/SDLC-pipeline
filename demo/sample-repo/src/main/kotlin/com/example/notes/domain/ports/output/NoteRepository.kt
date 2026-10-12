package com.example.notes.domain.ports.output

import com.example.notes.domain.model.Note
import com.example.notes.domain.model.NoteId

interface NoteRepository {
    fun save(note: Note): Note
    fun all(): List<Note>
}

fun interface IdGenerator {
    fun next(): NoteId
}
