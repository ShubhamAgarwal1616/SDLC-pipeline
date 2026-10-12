---
name: sdlc-feedback-resolver
description: Address human review comments on an AI-generated pull request, by changing code test-first and drafting a reply for each comment. Used as the pipeline's PR-feedback stage.
disable-model-invocation: true
---

# Resolve PR review comments

A human reviewed the pull request and left comments. Address every one.

## For each comment
1. Decide what it asks for: a behaviour change, a refactor, a test, a question, or something out of scope.
2. **Behaviour change:** write or adjust a failing test first, then change the code until it passes (follow the tdd skill).
3. **Refactor or style:** make the change and keep the tests green.
4. **Question:** answer it in the reply. Change code only if the answer reveals a problem.
5. **Disagree, or out of scope for this change:** don't silently ignore it. Explain briefly in the reply, and suggest a follow-up if useful.
6. If a comment changes **required behaviour**, also update the change's spec delta (`openspec/changes/<id>/specs/…`) so the spec matches the code. Add or adjust the WHEN/THEN scenario.

## Then
- Run the project's tests (offline) and keep them green.
- Commit in small steps, with messages that reference the story key.
- Write the replies file the stage contract names:
```json
{"replies": [{"comment_id": "rc-123 or ic-456", "body": "what you changed, or your answer (1-3 sentences)",
              "changed": true}],
 "spec_updated": false, "summary": "1-2 sentences"}
```
Every comment id you were given must have exactly one reply.
