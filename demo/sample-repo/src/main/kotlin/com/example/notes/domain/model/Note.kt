package com.example.notes.domain.model

@JvmInline
value class NoteId(val value: String)

/** The caller that owns a note, identified by its API key. */
@JvmInline
value class ClientKey(val value: String)

data class Note(val id: NoteId, val title: String, val body: String, val owner: ClientKey)

data class CreateNoteCommand(val owner: ClientKey, val title: String, val body: String)

sealed interface CreateNoteResult {
    data class Created(val note: Note) : CreateNoteResult
    data class Invalid(val reason: String) : CreateNoteResult
}

const val MAX_TITLE_LENGTH = 200
