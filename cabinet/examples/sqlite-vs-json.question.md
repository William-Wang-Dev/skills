Question: For a small personal command-line todo app, which storage should it use: (a) SQLite via Python's built-in sqlite3 module, or (b) one JSON file read and rewritten on each command?

Context: Python 3 stdlib only. Single user, one machine, roughly 50-2000 tasks over its life. Commands: add, list (with filter by tag/status), done, delete. Each invocation is a short-lived process. Users may occasionally run two commands at once from different terminals or scripts. Should be easy to back up and to inspect by hand. Maintained by one developer.

Answer shape: Verdict first (one line). Then up to 5 reasons with evidence, the strongest counter-argument to your own verdict, risks, and what would change your mind. Max ~300 words. Read-only: do not create or modify files.
