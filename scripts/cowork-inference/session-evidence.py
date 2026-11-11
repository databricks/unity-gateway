"""Assert completed Desktop tasks against their native session transcripts."""

import argparse
import json
import ntpath
import re
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
    listings = [
        record["attachment"].get("content", "")
        for record in records
        if isinstance(record, dict)
        and isinstance(record.get("attachment"), dict)
        and record["attachment"].get("type") == "skill_listing"
    ]
    inventory = re.findall(r"^- (.+?): ", listings[-1], re.MULTILINE) if listings else []
    matching_skills = [name for name in inventory if name.split(":")[-1] == "probe-summary"]
    messages = [
        record["message"]
        for record in records
        if isinstance(record, dict) and isinstance(record.get("message"), dict)
    ]
    start = next(
        (
            index
            for index, message in enumerate(messages)
            if message.get("role") == "user"
            and task["prompt"] in content_text(message.get("content", []))
        ),
        None,
    )
    if start is None:
        return {"status": "pending"}
    messages = messages[start:]
    if task.get("requireInventory") and listings and matching_skills != [task["skill"]]:
        return {
            "status": "failed",
            "error": "Expected exactly one native inventory entry for the skill",
            "matchingSkills": matching_skills,
        }
    answers = [
        content_text(message.get("content", []))
        for message in messages
        if message.get("role") == "assistant" and message.get("stop_reason") == "end_turn"
    ]
    answers = [answer for answer in answers if answer.strip()]
    if not answers:
        return {"status": "pending"}
    if task.get("requireInventory") and not listings:
        return {"status": "failed", "error": "Native skill inventory is missing"}
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
            and matches_reference(call.get("input", {}).get("file_path", ""), task)
            and task["expected"] in content_text(results[call["id"]].get("content", []))
        ),
        None,
    )
    if read is None:
        return {"status": "failed", "error": "No successful Read of the expected file and contents"}
    skills = [
        call
        for call in calls
        if call.get("name") == "Skill" and call.get("input", {}).get("skill") == task.get("skill")
    ]
    if task["capability"] == "skill" and len(skills) != 1:
        return {"status": "failed", "error": "Expected exactly one successful skill invocation"}
    skill = skills[0] if skills else None
    versions = sorted(
        {
            record["version"]
            for record in records
            if isinstance(record, dict) and record.get("version")
        }
    )
    session_ids = {
        record["sessionId"]
        for record in records
        if isinstance(record, dict) and record.get("sessionId")
    }
    hooks = []
    if task.get("requireHooks"):
        for path in Path(task["eventsDirectory"]).glob("*.json"):
            event = json.loads(path.read_text(encoding="utf-8"))
            if event.get("session_id") in session_ids:
                hooks.append(event)
        for event_name in ("SessionStart", "UserPromptSubmit"):
            if sum(event.get("hook_event_name") == event_name for event in hooks) != 1:
                return {
                    "status": "failed",
                    "error": f"Expected one {event_name} hook for the task session",
                    "hooks": hooks,
                }
    return {
        "status": "passed",
        "answer": answer,
        "file": read["input"]["file_path"],
        "readToolId": read["id"],
        "skillToolId": skill["id"] if skill else None,
        "engineVersions": versions,
        "hooks": hooks,
        "matchingSkills": matching_skills,
    }


def matches_reference(path, task):
    path = ntpath.normcase(ntpath.normpath(path))
    if path == ntpath.normcase(ntpath.normpath(task["expectedFile"])):
        return True
    return bool(task.get("allowPluginCache")) and path.endswith(
        r"\skills\probe-summary\references\facts.txt"
    )


def inspect_sessions(root, task):
    started = datetime.fromisoformat(task["startedAfter"]).timestamp()
    paths = []
    for path in root.glob("**/*.jsonl"):
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
    parser.add_argument("--sessions-root", type=Path, required=True, action="append")
    args = parser.parse_args()
    task = json.loads(args.task.read_text(encoding="utf-8-sig"))
    result = {"status": "pending"}
    for root in args.sessions_root:
        result = inspect_sessions(root, task)
        if result["status"] != "pending":
            break
    print(json.dumps(result))
