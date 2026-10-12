package com.example.notesclient.domain

data class Note(val id: String, val title: String, val body: String)

sealed interface ListNotesResult {
    data class Listed(val notes: List<Note>) : ListNotesResult
    data class Failed(val status: Int, val code: String, val message: String) : ListNotesResult
}

sealed interface CreateNoteResult {
    data class Created(val note: Note) : CreateNoteResult
    data class Rejected(val status: Int, val code: String, val message: String) : CreateNoteResult
}
