import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

from ucode.agents.desktop.claude import claude_desktop_directory, configure_claude_desktop
from ucode.databricks import list_anthropic_model_catalog
from ucode.desktop_auth import get_claude_desktop_relay_credential

WORKSPACE = "https://eng-ml-inference-team-us-east-1.cloud.databricks.com"
PROMPT = (
    "Reply with only the digits for forty-eight thousand two hundred thirteen. Do not use tools."
)


def redact(text):
    for name in ("DATABRICKS_BEARER", "CLAUDE_CODE_OAUTH_TOKEN"):
        if os.environ.get(name):
            text = text.replace(os.environ[name], "[REDACTED]")
    return text


def configure():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Use a disposable CI runner")
    case = json.loads(os.environ["UG_COMPAT_CASE"])
    folder = "macos-cowork-evidence" if sys.platform == "darwin" else "cowork-inference"
    evidence = Path(os.environ["RUNNER_TEMP"]) / folder
    evidence.mkdir(exist_ok=True)
    report = dict(case, workspace=WORKSPACE, configured=False)
    try:
        if os.environ["UCODE_TEST_WORKSPACE"].rstrip("/") != WORKSPACE:
            raise RuntimeError("CI credential workspace differs from the requested workspace")
        bearer = os.environ["DATABRICKS_BEARER"]
        config = Path.home() / ".databrickscfg"
        config.write_text(f"[desktop-ci]\nhost = {WORKSPACE}\nauth_type = databricks-cli\n")
        config.chmod(0o600)
        catalog = list_anthropic_model_catalog(WORKSPACE, bearer, provider=case["provider"])
        if case["model"] not in catalog.model_ids:
            raise RuntimeError(
                catalog.error_msg or "Selected model is not advertised to CI credentials"
            )
        args = [
            "-m",
            "ucode.cli",
            "claude-desktop-auth",
            "--host",
            WORKSPACE,
            "--profile",
            "desktop-ci",
        ]
        headers = {
            "Databricks-Model-Provider-Service": case["provider"],
            "x-databricks-use-coding-agent-mode": "true",
        }
        credential = {"token": bearer, "headers": {}}
        if case["relay"]:
            claude = shutil.which("claude")
            if not claude or not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
                raise RuntimeError(
                    "Relay requires Claude CLI and the existing CI subscription credential"
                )
            args += ["--relay", "--claude-binary", claude]
            credential = get_claude_desktop_relay_credential(WORKSPACE, "desktop-ci", claude)
        configure_claude_desktop(
            model_ids=[case["model"]],
            model_id_to_display_name=catalog.model_id_to_display_name,
            base_url=WORKSPACE + "/ai-gateway/anthropic",
            helper_command=sys.executable,
            helper_args=args,
            custom_headers=headers,
            profile_key="desktop-ci",
        )
        directory = claude_desktop_directory()
        profile = json.loads((directory / "5b36a0b9-b56d-47cf-b6a4-0904e0bc14f6.json").read_text())
        assert profile["inferenceProvider"] == "gateway"
        assert (
            profile["inferenceCustomHeaders"]["Databricks-Model-Provider-Service"]
            == case["provider"]
        )
        assert [m["name"] for m in profile["inferenceModels"]] == [case["model"]]
        assert ("--relay" in profile["inferenceCredentialHelperArgs"]) == case["relay"]
        label = profile["inferenceModels"][0].get("labelOverride") or case["model"]
        report.update(configured=True, targetModel=case["model"], targetModelLabel=label)
        api = {}
        request = urllib.request.Request(
            WORKSPACE + "/ai-gateway/anthropic/v1/messages",
            data=json.dumps(
                {
                    "model": case["model"],
                    "max_tokens": 64,
                    "messages": [{"role": "user", "content": PROMPT}],
                }
            ).encode(),
            headers={
                **headers,
                **credential["headers"],
                "Authorization": "Bearer " + credential["token"],
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.load(response)
                answer = "".join(p.get("text", "") for p in payload.get("content", []))
                api.update(
                    status=response.status, response=answer, passed=answer.strip() == "48213"
                )
        except urllib.error.HTTPError as exc:
            api.update(
                status=exc.code, error=exc.read().decode(errors="replace")[:2000], passed=False
            )
        except Exception as exc:
            api.update(error=str(exc), passed=False)
        (evidence / "direct-api.json").write_text(redact(json.dumps(api, indent=2)))
        return report
    except Exception as exc:
        report["error"] = str(exc)
        raise
    finally:
        (evidence / "configuration.json").write_text(redact(json.dumps(report, indent=2)))


if __name__ == "__main__":
    configure()
