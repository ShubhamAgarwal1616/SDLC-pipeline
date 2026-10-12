package com.example.notes.services

import com.example.notes.domain.model.CreateNoteCommand
import com.example.notes.domain.model.CreateNoteResult
import com.example.notes.domain.model.MAX_TITLE_LENGTH
import com.example.notes.domain.model.Note
import com.example.notes.domain.ports.output.IdGenerator
import com.example.notes.domain.ports.output.NoteRepository

class CreateNote(private val notes: NoteRepository, private val ids: IdGenerator) {
    fun execute(command: CreateNoteCommand): CreateNoteResult = when {
        command.title.isBlank() -> CreateNoteResult.Invalid("title must not be blank")
        command.title.length > MAX_TITLE_LENGTH ->
            CreateNoteResult.Invalid("title must be at most $MAX_TITLE_LENGTH characters")
        else -> CreateNoteResult.Created(
            notes.save(Note(ids.next(), command.title.trim(), command.body, command.owner)),
        )
    }
}
