"""Assert completed Desktop tasks against their native session transcripts."""

import argparse
import json
import ntpath
import posixpath
from datetime import datetime
from pathlib import Path


def content_blocks(content):
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def content_text(content):
    if isinstance(content, str):
        return content
    return "\n".join(
        block["text"]
        for block in content_blocks(content)
        if block.get("type") == "text" and isinstance(block.get("text"), str)
    )


def evaluate(records, task):
    paths = posixpath if task.get("platform") == "macos" else ntpath
    messages = [
        record["message"]
        for record in records
        if isinstance(record, dict) and isinstance(record.get("message"), dict)
    ]
    if not any(
        message.get("role") == "user" and task["prompt"] in content_text(message.get("content", []))
        for message in messages
    ):
        return {"status": "pending"}
    answers = [
        content_text(message.get("content", []))
        for message in messages
        if message.get("role") == "assistant" and message.get("stop_reason") == "end_turn"
    ]
    answers = [answer for answer in answers if answer.strip()]
    if not answers:
        return {"status": "pending"}
    answer = answers[-1].strip()
    if answer != task["expected"]:
        return {
            "status": "failed",
            "error": "Completed answer does not match the file",
            "answer": answer,
        }
    blocks = [block for message in messages for block in content_blocks(message.get("content"))]
    results = {
        block["tool_use_id"]: block
        for block in blocks
        if block.get("type") == "tool_result" and block.get("tool_use_id")
    }
    calls = [
        block
        for block in blocks
        if block.get("type") == "tool_use"
        and block.get("id") in results
        and not results[block["id"]].get("is_error")
    ]
    read = next(
        (
            call
            for call in calls
            if call.get("name") == "Read"
            and paths.normcase(paths.normpath(call.get("input", {}).get("file_path", "")))
            == paths.normcase(paths.normpath(task["expectedFile"]))
            and task["expected"] in content_text(results[call["id"]].get("content", []))
        ),
        None,
    )
    if read is None:
        return {"status": "failed", "error": "No successful Read of the expected file and contents"}
    skill = next(
        (
            call
            for call in calls
            if call.get("name") == "Skill"
            and call.get("input", {}).get("skill") == task.get("skill")
        ),
        None,
    )
    if task["capability"] == "skill" and skill is None:
        return {"status": "failed", "error": "No successful invocation of the expected skill"}
    return {
        "status": "passed",
        "answer": answer,
        "file": read["input"]["file_path"],
        "readToolId": read["id"],
        "skillToolId": skill["id"] if skill else None,
    }


def inspect_sessions(root, task):
    started = datetime.fromisoformat(task["startedAfter"]).timestamp()
    paths = []
    for path in root.glob("**/projects/session/*.jsonl"):
        try:
            modified = path.stat().st_mtime
        except FileNotFoundError:
            continue
        if modified >= started:
            paths.append((modified, path))
    for _, path in sorted(paths, reverse=True):
        try:
            text = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            continue
        records = []
        for line in text.splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # The running app can still be appending the final record.
        result = evaluate(records, task)
        if result["status"] != "pending":
            return {**result, "transcript": str(path)}
    return {"status": "pending"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--sessions-root", type=Path, required=True)
    args = parser.parse_args()
    task = json.loads(args.task.read_text(encoding="utf-8-sig"))
    print(json.dumps(inspect_sessions(args.sessions_root, task)))
