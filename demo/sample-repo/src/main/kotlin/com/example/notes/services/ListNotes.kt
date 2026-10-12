package com.example.notes.services

import com.example.notes.domain.model.Note
import com.example.notes.domain.ports.output.NoteRepository

class ListNotes(private val notes: NoteRepository) {
    fun execute(): List<Note> = notes.all()
}
