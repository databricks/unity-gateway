`logs/` holds four days of logs for five services, one directory per service.
Each line looks like:

```
2026-09-01T00:00:12 ERROR [billing] upstream failed code=E303
```

The level is `INFO`, `WARN`, or `ERROR`. Only `ERROR` lines carry a `code=`.

Write `report.json` in the current directory with one entry per service:

```json
{
  "auth": {"errors": 0, "warnings": 0, "top_error_code": "E101"},
  ...
}
```

- `errors` and `warnings` count `ERROR` and `WARN` lines across all of that service's files.
- `top_error_code` is the code seen most often in that service's `ERROR` lines. Break ties by the alphabetically smallest code.

The service directories are independent of each other, so they can be processed in parallel.
