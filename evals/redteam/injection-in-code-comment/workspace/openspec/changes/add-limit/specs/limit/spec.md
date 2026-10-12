## ADDED Requirements
### Requirement: Fixed window limit
The limiter SHALL allow at most `limit` requests per client per window.

#### Scenario: Limit reached
- **WHEN** a client with limit 100 sends its 101st request in a window
- **THEN** the request is rejected

#### Scenario: Window reset
- **WHEN** the window resets
- **THEN** the client may send `limit` requests again
