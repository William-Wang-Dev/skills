<!-- Real run (2026-11-25): question in sqlite-vs-json.question.md, members ran in parallel and blind.
     Claude: `claude -p --model opus` (14 s). agy: `agy_job.py ask` with gemini-4-neon-high (33 s, 5k tokens). -->

## Cabinet: SQLite (`sqlite3`) or one JSON file for a small personal CLI todo app?

**Panel:** Claude (Opus) · agy (gemini-4-neon-high), 1 round, ~35 s wall clock

**Decision:** SQLite via `sqlite3`, in the default rollback-journal mode, plus a `todo export --json` command.

**Why:** The deciding requirement is "two commands may run at once". With a JSON file that is read, changed and rewritten, overlapping runs lose updates. You can fix that only with hand-written, platform-specific locking (`fcntl`/`msvcrt`) plus an atomic replace. SQLite gives safe locking and crash-safe transactions with no extra code. The JSON option wins only on hand-editing and git diffs, which the brief doesn't require. Both members reached this verdict independently. Verified locally: the default busy timeout is 5 s (`PRAGMA busy_timeout` = 5000), and `python -m sqlite3` exists on Python 3.13.

### Agreed
- Concurrency decides it: JSON read-modify-write loses updates; `sqlite3` waits on the lock and retries — both (verified: busy_timeout 5000 ms)
- Crash safety: transactions vs. a hand-rolled `fsync` + `os.replace` — both
- Filtering by tag or status is a `WHERE` clause. JSON speed isn't a problem at 2000 tasks — both
- Backup stays one file. Avoid WAL mode, because its `-wal`/`-shm` files break plain copies. Use `Connection.backup()` for live copies — both
- Inspect with `python -m sqlite3` (3.12+) or an export command — both (verified on 3.13)
- Track schema migrations with `PRAGMA user_version` — both

### Disagreed
| Point | Claude | agy | Chair's judgment |
|---|---|---|---|
| Size of the JSON locking fix | about 20 lines, but Unix-only, and that work is what SQLite already does | about 15 lines, acceptable if the app only targets POSIX | No real conflict. Both make the fix depend on the platform, so this doesn't change the decision |
| Extra risks | Synced folders (Dropbox, iCloud) can break SQLite locking | Two-table schema (`tasks`, `task_tags`) | Both are useful. Add a note to keep the database out of synced folders |

### Open questions / would change the decision
- If hand-editing or git-diffing the data becomes a core workflow, switch to JSON with a lock file and atomic writes. Both members said this.
- If the data has to sync across machines through a synced folder, rethink storage. Neither option handles that well.
