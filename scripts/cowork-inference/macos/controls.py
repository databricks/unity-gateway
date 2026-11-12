import json


def matches_labels(snapshot, labels, *, require_all=False, enabled=False):
    if isinstance(labels, str):
        labels = [labels]
    rows = json.loads(snapshot)
    fields = {
        field
        for row in rows
        if len(row) == 7 and (not enabled or row[-2] == "1")
        for field in row[1:5]
    }
    found = [label in fields for label in labels]
    return bool(found) and (all(found) if require_all else any(found))


def first_label_with_prefix(snapshot, prefix):
    for row in json.loads(snapshot):
        if len(row) == 7 and row[-2] == "1":
            for field in row[1:5]:
                if field.startswith(prefix):
                    return field
    return None
