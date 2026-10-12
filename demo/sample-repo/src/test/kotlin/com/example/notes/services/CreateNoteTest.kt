package com.example.notes.services

import com.example.notes.domain.model.ClientKey
import com.example.notes.domain.model.CreateNoteCommand
import com.example.notes.domain.model.CreateNoteResult
import com.example.notes.domain.model.MAX_TITLE_LENGTH
import com.example.notes.domain.model.Note
import com.example.notes.domain.model.NoteId
import com.example.notes.domain.ports.output.NoteRepository
import io.kotest.core.spec.style.DescribeSpec
import io.kotest.matchers.shouldBe
import io.kotest.matchers.types.shouldBeInstanceOf

private class FakeNotes : NoteRepository {
    val saved = mutableListOf<Note>()
    override fun save(note: Note) = note.also { saved += it }
    override fun all() = saved.toList()
}

class CreateNoteTest : DescribeSpec({
    val owner = ClientKey("key-1")

    describe("CreateNote") {
        it("creates a note with a trimmed title") {
            val notes = FakeNotes()
            val result = CreateNote(notes) { NoteId("n1") }.execute(CreateNoteCommand(owner, "  Shopping  ", "milk"))
            result shouldBe CreateNoteResult.Created(Note(NoteId("n1"), "Shopping", "milk", owner))
            notes.saved.size shouldBe 1
        }

        it("rejects a blank title") {
            val notes = FakeNotes()
            CreateNote(notes) { NoteId("n1") }.execute(CreateNoteCommand(owner, "   ", ""))
                .shouldBeInstanceOf<CreateNoteResult.Invalid>()
            notes.saved.size shouldBe 0
        }

        it("rejects a title that is too long") {
            val result = CreateNote(FakeNotes()) { NoteId("n1") }
                .execute(CreateNoteCommand(owner, "x".repeat(MAX_TITLE_LENGTH + 1), ""))
            result.shouldBeInstanceOf<CreateNoteResult.Invalid>()
        }
    }
})
