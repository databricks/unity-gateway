def matches_labels(snapshot, labels, *, require_all=False, enabled=False):
    if isinstance(labels, str):
        labels = [labels]
    rows = [row.split(" | ") for row in snapshot.splitlines()]
    fields = {
        field
        for row in rows
        if len(row) == 7 and (not enabled or row[-2] == "1")
        for field in row[1:5]
    }
    found = [label in fields for label in labels]
    return bool(found) and (all(found) if require_all else any(found))
