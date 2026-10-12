"""Managed-skill checks shared by the CUJs that publish a skills selector."""

from tests.integration.utils.evidence import assistant_answer_contains
from tests.integration.utils.terminal import AgentTerminal

from .constants import CLAUDE, CODEX, FIXTURE_SUMMARY_SKILL_NAME, INFERENCE_PATHS

# Claude reads the first root and Codex the second; ug writes managed skills to both.
SKILL_ROOTS = (".claude/skills", ".agents/skills")


def skill_name(fqn: str) -> str:
    """The securable leaf, which is also the bundle directory and the name agents list."""
    return fqn.rsplit(".", 1)[1]


FIXTURE_SUMMARY = skill_name(FIXTURE_SUMMARY_SKILL_NAME)
# Only reference/fixture-facts.md holds the token, so a reply with it proves the bundle was read.
FIXTURE_SUMMARY_TOKEN = "ugsum-4c1e9a"


def skills_view(
    session,
    recorder,
    agent: str,
    listed: tuple[str, ...],
    name: str | None = None,
    codex_filter: str = "",
) -> str:
    """The agent's /skills screen once every ``listed`` skill renders, so absence checks are real.

    Codex's skill popup shows eight rows, so ``codex_filter`` narrows it; it must also match any
    skill the caller asserts is absent.
    """
    recorder.prepare_launch()
    with AgentTerminal(
        session, agent, [str(session.binary), agent], name or f"{agent}-skills"
    ) as tui:
        tui.boot()
        tui.send("/skills", "type the /skills command")
        tui.send("\r", "open the skills list")
        if agent == CODEX:
            tui.send("\r", "confirm the skills selection so the list renders")
            if codex_filter:
                tui.send(codex_filter, "filter the skills popup")
        tui.wait_for(
            lambda s: all(name in s for name in listed),
            "the /skills view to list skills",
            timeout=60,
        )
        return tui.visible


def bundles_on_disk(session, root: str) -> set[str]:
    return {path.name for path in (session.home / root).iterdir()}


def bundle_files(session, root: str, bundle: str) -> dict[str, bytes]:
    base = session.home / root / bundle
    return {
        str(path.relative_to(base)): path.read_bytes() for path in base.rglob("*") if path.is_file()
    }


def assert_fixture_summary_invoked(session, recorder, agent: str) -> None:
    """Run a headless prompt that must load fixture-summary and answer from its reference file."""
    recorder.prepare_launch()
    # Codex's `$name` mention loads the skill explicitly; small models skip implicit skill matches.
    skill = f"${FIXTURE_SUMMARY}" if agent == CODEX else f"the {FIXTURE_SUMMARY} skill"
    prompt = (
        f"Use {skill} and reply with only the one-line summary it produces. Do not use subagents."
    )
    checkpoint = recorder.checkpoint()
    if agent == CLAUDE:
        session.run(
            CLAUDE,
            "--",
            "-p",
            prompt,
            "--output-format",
            "json",
            "--allowedTools",
            "Skill,Read",
            timeout=240,
        )
    else:
        session.run(CODEX, "--", "exec", "--skip-git-repo-check", "--json", prompt, timeout=240)
    assert assistant_answer_contains(session, agent, FIXTURE_SUMMARY_TOKEN), (
        f"{agent} never answered with the token from the skill's reference file"
    )
    request = recorder.expect_request(
        method="POST", path=INFERENCE_PATHS[agent], after=checkpoint, timeout=240
    )
    assert recorder.response_for(request, timeout=240).status_code == 200
