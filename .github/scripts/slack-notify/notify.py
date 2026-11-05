"""Post one success or failure message for a scheduled test workflow to Slack."""

import json
import os
import urllib.error
import urllib.request

# Slack member IDs for lilly-luo_data and rohit-a_data.
OWNERS = "<@U09DPGC3DFY> <@U09EFHFRT24>"


def main():
    name = os.environ["NOTIFY_NAME"]
    versions = (
        f"Versions used: Claude Code {os.environ['CLAUDE_VERSION'] or 'unknown'}, "
        f"Codex: {os.environ['CODEX_VERSION'] or 'unknown'}"
    )
    repository = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["GITHUB_RUN_ID"]
    run_url = f"{os.environ['GITHUB_SERVER_URL']}/{repository}/actions/runs/{run_id}"
    if os.environ["NOTIFY_RESULT"] == "success":
        message = f"✅ {name} succeeded. {versions}. Link to run {run_url}"
    else:
        request = urllib.request.Request(
            f"{os.environ['GITHUB_API_URL']}/repos/{repository}/actions/runs/{run_id}/jobs?filter=latest&per_page=100",
            headers={"Authorization": f"Bearer {os.environ['GH_TOKEN']}"},
        )
        failed_url = run_url
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                jobs = json.load(response)["jobs"]
            failed_url = next(
                (
                    job["html_url"]
                    for job in jobs
                    if job["conclusion"] not in (None, "success", "skipped", "cancelled")
                ),
                failed_url,
            )
        except urllib.error.URLError:
            print("Unable to resolve failed job URL; linking to the workflow run.")
        message = f"❌ ALERT! {name} failed. {versions}. Link to failed job: {failed_url} {OWNERS}"
    print(message)
    request = urllib.request.Request(
        os.environ["SLACK_WEBHOOK"],
        data=json.dumps({"text": message}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


if __name__ == "__main__":
    main()
