The git repository in the current directory leaked an API token (it starts with `ugb_live_`). It was committed on `main` and later removed, and it also shows up on the `feature/report` branch.

Rewrite the history so the token is unreachable from every ref:

- Replace every occurrence of the token, in every commit on every branch, with the literal text `REDACTED`. Keep the rest of each file as is.
- Keep both branches, the same number of commits on each, and the same commit messages in the same order.
- Don't leave backup refs (such as `refs/original/`) that still contain the token.
- Leave `main` checked out when you are done.
