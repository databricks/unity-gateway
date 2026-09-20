"""Download Unity Catalog skills and write them to disk, one flat dir per skill."""

from __future__ import annotations

import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

import questionary

from ucode.databricks import get_databricks_token, workspace_org_id
from ucode.mcp import register_schemaless_skills_connection, setup_mcp_clients
from ucode.skills_api import (
    _SKILLS_WALK_DEADLINE_SECONDS,
    _SKILLS_WALK_TIMEOUT_REASON,
    SkillRef,
    fetch_skill_bundle,
    get_skill,
    list_all_skills,
    list_schema_skills,
)
from ucode.skills_state import (
    SKILL_UPDATE_CHECK_INTERVAL,
    SkillInstall,
    forget,
    last_update_check,
    list_downloaded,
    record_downloads,
    records_for_fqns,
    records_for_schema,
    records_for_scope,
    remove_downloads,
    set_last_update_check,
)
from ucode.state import load_state
from ucode.ui import (
    console,
    picker_style,
    print_note,
    print_success,
    print_warning,
    progress_bar,
    prompt_yes_no,
    scrolling_checkbox,
)

# `.claude/skills` (Claude) + `.agents/skills` (the alias other agents read).
SKILL_BASE_DIR_NAMES = (".claude/skills", ".agents/skills")

# Parallel skill fetches per schema; writes stay sequential (they prompt).
_MAX_FETCH_WORKERS = 8


# --- On-disk writer --------------------------------------------------------


def skill_dir_roots(project_dir: str | None) -> list[Path]:
    """The ``.claude/skills`` and ``.agents/skills`` roots to download into.

    ``project_dir`` must be an existing absolute directory when given; when
    omitted, roots default to the user's home directory (user scope).
    """
    if project_dir is None:
        base = Path.home()
    else:
        base = Path(project_dir)
        if not base.is_absolute():
            raise ValueError(f"--path must be an absolute path, got `{project_dir}`.")
        if not base.is_dir():
            raise ValueError(f"--path directory does not exist: `{project_dir}`.")
    return [base / name for name in SKILL_BASE_DIR_NAMES]


def _safe_relative_path(relative_path: str) -> Path | None:
    """A bundle file's path within its skill dir, or None if it escapes the dir.

    The Files API returns server-controlled paths, but ucode writes them to
    disk, so reject absolute paths and any ``..`` traversal.
    """
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        return None
    return path


def _write_bundle(skill_dir: Path, bundle_name: str, files: dict[str, bytes]) -> None:
    for relative_path, content in files.items():
        safe_path = _safe_relative_path(relative_path)
        if safe_path is None:
            print_warning(f"Skipping unsafe path in `{bundle_name}`: {relative_path}")
            continue
        destination = skill_dir / safe_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


def existing_skill_on_disk(roots: list[Path], bundle_name: str) -> bool:
    """Whether ``bundle_name`` already has a skill directory under any root."""
    return any((root / bundle_name).exists() for root in roots)


def should_download_skill(roots: list[Path], ref: SkillRef) -> bool:
    """Whether ``ref`` should be fetched and written into ``roots``.

    Applies the disk-only check that needs no bundle bytes: prompts before
    overwriting a skill already on disk (naming the source by ``ref.fqn``), so a
    declined skill is never fetched. Dedup keys on the bundle name, since that is
    the directory an agent would load. Name validity is the server's job --
    FinalizeSkill enforces the Agent Skills naming rules on ``bundle_name`` before
    we ever see it -- so ucode does not re-check it here.
    """
    if existing_skill_on_disk(roots, ref.bundle_name) and not prompt_yes_no(
        f"A skill named `{ref.bundle_name}` already exists. Overwrite it with `{ref.fqn}`?"
    ):
        print_note(f"Kept existing `{ref.bundle_name}`.")
        return False

    return True


def write_skill(roots: list[Path], ref: SkillRef, files: dict[str, bytes]) -> None:
    """Write ``ref``'s bundle (``{relpath: bytes}``) into every root.

    The directory is named for the bundle, so it matches the ``name:`` an agent
    reads from the written SKILL.md.
    """
    for root in roots:
        _write_bundle(root / ref.bundle_name, ref.bundle_name, files)


def _skill_installs(
    refs: list[SkillRef],
    roots: list[Path],
    path: str | None,
    workspace: str,
    *,
    scope: str | None = None,
) -> list[SkillInstall]:
    """Attribution records for ``refs`` written into ``roots`` (see ``skills_state``).

    ``scope`` overrides the download scope; when omitted it is ``project`` for a ``--path``
    download and ``user`` otherwise. Admin-published (managed) downloads pass ``scope="managed"``
    so they stay distinguishable from a developer's own downloads, which lets ``ug configure``
    reconcile them and keeps ``ug skills remove`` from dropping them.
    """
    base = path or str(Path.home())
    resolved_scope = scope or ("project" if path else "user")
    org_id = workspace_org_id(workspace)
    return [
        SkillInstall(
            fqn=ref.fqn,
            bundle_name=ref.bundle_name,
            workspace=workspace,
            scope=resolved_scope,
            base=base,
            dirs=tuple(str(root / ref.bundle_name) for root in roots),
            metastore_id=ref.metastore_id,
            workspace_id=org_id,
            skill_id=ref.skill_id,
            uc_update_time=ref.uc_update_time,
        )
        for ref in refs
    ]


# --- Orchestration ---------------------------------------------------------


def _fetch_bundles(
    workspace: str, token: str, refs: list[SkillRef], *, label: str
) -> dict[str, tuple[dict[str, bytes] | None, str | None]]:
    """Fetch every skill's bundle concurrently, keyed by FQN.

    Renders a ``k/n`` progress bar labeled ``label`` that advances as each fetch
    completes. Keying on the FQN keeps a cross-schema batch's securables apart,
    since a securable name is unique only within its own schema.
    """
    if not refs:
        return {}
    results: dict[str, tuple[dict[str, bytes] | None, str | None]] = {}
    with (
        progress_bar(label, len(refs)) as advance,
        ThreadPoolExecutor(max_workers=min(_MAX_FETCH_WORKERS, len(refs))) as pool,
    ):
        futures = {
            pool.submit(
                fetch_skill_bundle, workspace, token, ref.catalog, ref.schema, ref.securable_name
            ): ref.fqn
            for ref in refs
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()
            advance()
    return results


def _reject_bundle_name_collisions(refs: list[SkillRef]) -> list[SkillRef]:
    """``refs`` with any later skill that repeats an earlier one's bundle name dropped.

    Only the securable name is unique within a schema; ``bundle_name`` comes from
    each bundle's SKILL.md frontmatter and is never checked against its siblings,
    so two skills can claim the same directory. Writing both would land them on top
    of each other, leaving whichever finished last with no sign the other was lost,
    so keep the first and warn about the rest by FQN.
    """
    kept: list[SkillRef] = []
    claimed: dict[str, SkillRef] = {}
    for ref in refs:
        winner = claimed.get(ref.bundle_name)
        if winner is not None:
            print_warning(
                f"Skipping `{ref.fqn}`: its bundle name `{ref.bundle_name}` is already "
                f"claimed by `{winner.fqn}`. Rename one skill's SKILL.md `name:` to download both."
            )
            continue
        claimed[ref.bundle_name] = ref
        kept.append(ref)
    return kept


def _fetch_and_write(
    workspace: str, token: str, refs: list[SkillRef], roots: list[Path], *, label: str
) -> list[SkillRef]:
    """Fetch each ref's bundle concurrently, write it into ``roots``, and return those that
    reached disk. A per-skill fetch failure or disk error warns and skips only that skill."""
    if not refs:
        return []
    bundles = _fetch_bundles(workspace, token, refs, label=label)
    written: list[SkillRef] = []
    for ref in refs:
        files, reason = bundles[ref.fqn]
        if reason or files is None:
            print_warning(f"Skipping `{ref.fqn}`: {reason}.")
            continue
        try:
            write_skill(roots, ref, files)
        except OSError as exc:
            print_warning(f"Skipping `{ref.fqn}`: {exc}.")
            continue
        written.append(ref)
    return written


def _download_refs(
    workspace: str, token: str, refs: list[SkillRef], roots: list[Path], *, label: str
) -> tuple[list[SkillRef], int]:
    """Fetch and write ``refs`` into ``roots``, returning ``(written, total)``.

    The shared download core: drop siblings claiming one directory
    (``_reject_bundle_name_collisions``), prompt before overwriting a skill already
    on disk (``should_download_skill``, so a declined skill is never fetched), then
    fetch and write the survivors. ``written`` are the refs that reached disk, so a
    caller can record their attribution; ``total`` is the count that could reach disk
    (dropped siblings excluded), so a caller's summary denominator is right.
    """
    refs = _reject_bundle_name_collisions(refs)
    to_download = [ref for ref in refs if should_download_skill(roots, ref)]
    written = _fetch_and_write(workspace, token, to_download, roots, label=label)
    console.print()
    return written, len(refs)


def download_skills_from_schema_locations(
    workspace: str,
    token: str,
    locations: list[str],
    path: str | None,
) -> None:
    """Download every skill in each ``<catalog>.<schema>`` location to disk.

    Locations are processed one at a time. Each lists the schema's finalized
    skills, then hands the refs to ``_download_refs`` and prints a per-location
    summary. Finishing one location before the next means a skill written for an
    earlier location is already on disk when a same-named skill in a later location
    reaches the overwrite prompt, so the prompt still fires. Downloading a named
    subset instead of whole schemas is a separate path (``download_selected_skills``
    over fully-qualified names).
    """
    roots = skill_dir_roots(path)
    roots_display = " and ".join(str(root) for root in roots)
    for location in locations:
        catalog, schema = location.split(".")
        refs, reason = list_schema_skills(workspace, token, catalog, schema)
        if reason:
            print_warning(f"Skipping `{location}`: {reason}.")
            continue
        if not refs:
            print_note(f"No skills found in `{location}`.")
            continue
        written, total = _download_refs(
            workspace, token, refs, roots, label=f"Fetching skills from {location}"
        )
        record_downloads(_skill_installs(written, roots, path, workspace))
        count = len(written)
        skipped = f"; {total - count} skipped" if count < total else ""
        print_success(
            f"Downloaded {count}/{total} skill(s){skipped} from `{location}` in {roots_display}."
        )


def download_selected_skills(workspace: str, token: str, fqns: list[str], path: str | None) -> None:
    """Download the skills named by ``fqns`` (``<catalog>.<schema>.<name>``) to disk.

    Resolves each FQN with ``GetSkill`` (a skill that cannot be downloaded warns and
    is skipped), then hands the flat, possibly cross-schema set to ``_download_refs``
    in one pass, so collisions are deduped across the whole selection under a single
    summary.
    """
    roots = skill_dir_roots(path)
    roots_display = " and ".join(str(root) for root in roots)
    refs: list[SkillRef] = []
    for fqn in fqns:
        ref = get_skill(workspace, token, fqn)
        if ref is None:
            print_warning(f"Skipping `{fqn}`: not a downloadable skill.")
            continue
        refs.append(ref)
    written, total = _download_refs(workspace, token, refs, roots, label="Fetching selected skills")
    record_downloads(_skill_installs(written, roots, path, workspace))
    count = len(written)
    skipped = f"; {total - count} skipped" if count < total else ""
    print_success(f"Downloaded {count}/{total} skill(s){skipped} in {roots_display}.")


def _resolve_managed_skills(
    workspace: str, token: str, selector: dict
) -> tuple[list[SkillRef], set[str] | None]:
    """Resolve a managed ``skills`` selector into ``(refs_to_download, desired_fqns)``.

    ``selector`` is the normalized ``NamesOrLocation`` (``{names?, unity_catalog_location?}``).
    ``desired_fqns`` is the set of FQNs the config wants on disk -- the reconcile removes any managed
    skill outside it -- and is ``None`` when that set can't be determined (a malformed or unlistable
    ``unity_catalog_location``), so the caller removes nothing rather than acting on a partial view.
    An empty set means the config authoritatively wants no managed skills.

    A ``unity_catalog_location`` lists every finalized skill under that ``<catalog>.<schema>``, so
    the listing is both the download set and the desired set. ``names`` are full
    ``<catalog>.<schema>.<name>`` FQNs: they form the desired set directly, so a name that currently
    can't be fetched is still desired and is never reconciled away. Mirrors
    :func:`ucode.mcp._resolve_managed_mcp_servers`.
    """
    location = selector.get("unity_catalog_location")
    if isinstance(location, str) and location:
        if location.count(".") != 1 or not all(part for part in location.split(".")):
            print_warning(
                f"Skipping managed skills location `{location}`: expected `<catalog>.<schema>`."
            )
            return [], None
        catalog, schema = location.split(".")
        refs, reason = list_schema_skills(workspace, token, catalog, schema)
        if reason:
            print_warning(f"Could not list workspace skills in `{location}`: {reason}.")
            return [], None
        return refs, {ref.fqn for ref in refs}
    names = [n for n in (selector.get("names") or []) if isinstance(n, str) and n]
    malformed = sorted(
        n for n in names if n.count(".") != 2 or not all(part for part in n.split("."))
    )
    if malformed:
        print_warning(
            "Skipping managed skills name(s) that aren't full "
            f"`<catalog>.<schema>.<name>` names: {', '.join(malformed)}."
        )
    wanted = [n for n in names if n not in set(malformed)]
    refs: list[SkillRef] = []
    seen: set[str] = set()
    for fqn in wanted:
        if fqn in seen:
            continue
        seen.add(fqn)
        ref = get_skill(workspace, token, fqn)
        if ref is None:
            print_warning(f"Skipping `{fqn}`: not a downloadable skill.")
            continue
        refs.append(ref)
    return refs, set(wanted)


def reconcile_managed_skills(managed: dict) -> tuple[list[str], list[str]]:
    """Make the developer's on-disk skills match the managed config's ``skills`` selector.

    Called from ``ug configure`` once the enabled agents are configured, mirroring
    :func:`ucode.mcp.reconcile_managed_mcp_servers`. Downloads any desired skill not already on disk
    (additive, no overwrite prompt) and removes the managed skills the config no longer lists.
    Removals are driven by attribution (``scope="managed"`` in the skills manifest), so a developer's
    own skills are never touched, and are skipped when the desired set can't be determined, so a
    transient listing failure never deletes one. Bundles land in both ``.claude/skills`` and
    ``.agents/skills``, so Claude Code and Codex both pick them up. Returns ``(written, removed)``
    bundle names. Raises ``RuntimeError`` on an auth or discovery failure; the caller stays best-effort.
    """
    selector = managed.get("skills")
    selector = selector if isinstance(selector, dict) else {}
    state = load_state()
    workspace = state.get("workspace")
    if not workspace:
        raise RuntimeError("Workspace is not configured. Run `ucode configure` first.")
    # An empty selector only removes prior managed skills, a local no-auth op, so skip the token.
    token = get_databricks_token(workspace, state.get("profile")) if selector else ""
    roots = skill_dir_roots(None)
    base = str(Path.home())
    refs, desired = _resolve_managed_skills(workspace, token, selector)
    refs = _reject_bundle_name_collisions(refs)

    removed: list[str] = []
    if desired is not None:
        stale = [r for r in records_for_scope("managed", base) if r.get("fqn") not in desired]
        if stale:
            remove_downloads(stale)
            removed = [str(r["bundle_name"]) for r in stale if r.get("bundle_name")]

    missing = [ref for ref in refs if not existing_skill_on_disk(roots, ref.bundle_name)]
    installed = _fetch_and_write(
        workspace, token, missing, roots, label="Fetching workspace skills"
    )
    record_downloads(_skill_installs(installed, roots, None, workspace, scope="managed"))
    return [ref.bundle_name for ref in installed], removed


# --- Launch-time refresh ---------------------------------------------------


def _eligible_launch_records(records: list[dict], workspace: str) -> list[dict]:
    """The current workspace's own (non-managed) downloads, which a launch may refresh.

    Managed skills are left to ``ug configure``, and other workspaces' downloads are skipped
    because the launch token authenticates only this workspace.
    """
    return [
        record
        for record in records
        if record.get("scope") != "managed" and record.get("workspace") == workspace
    ]


def _get_updated_refs(
    workspace: str, token: str, records: list[dict]
) -> list[tuple[dict, SkillRef]]:
    """Pair each record whose UC source is newer than its download with the current skill.

    Resolves every record concurrently; one that no longer resolves (deleted, unfinalized,
    unauthorized) is skipped, leaving its on-disk copy alone. A record with no recorded
    ``uc_update_time`` predates attribution, so it is refreshed once to backfill the field.
    """
    if not records:
        return []
    pairs: list[tuple[dict, SkillRef]] = []
    with ThreadPoolExecutor(max_workers=min(_MAX_FETCH_WORKERS, len(records))) as pool:
        futures = {pool.submit(get_skill, workspace, token, r["fqn"]): r for r in records}
        for future in as_completed(futures):
            ref = future.result()
            if ref is None:
                continue
            record = futures[future]
            stored = record.get("uc_update_time")
            if stored is None or (ref.uc_update_time or "") > stored:
                pairs.append((record, ref))
    return pairs


def _update_stale_skills(workspace: str, token: str, pairs: list[tuple[dict, SkillRef]]) -> int:
    """Re-download each stale skill into its own base and refresh its manifest record.

    Overwrites in place with no prompt, since the developer already chose to download these,
    and only manifest-attributed directories are touched, so a user-authored skill of the same
    name is never overwritten. Returns how many skills were rewritten.
    """
    home = os.path.normpath(str(Path.home()))
    refs_by_base: dict[str, list[SkillRef]] = {}
    for record, ref in pairs:
        refs_by_base.setdefault(os.path.normpath(record["base"]), []).append(ref)

    updated = 0
    for base, refs in refs_by_base.items():
        path = None if base == home else base
        roots = skill_dir_roots(path)
        written = _fetch_and_write(workspace, token, refs, roots, label="Updating skills")
        record_downloads(_skill_installs(written, roots, path, workspace))
        updated += len(written)
    return updated


def refresh_downloaded_skills_on_launch(state: dict) -> None:
    """Update downloaded skills whose UC source changed, before an agent launches.

    Rate-limited to once per ``SKILL_UPDATE_CHECK_INTERVAL`` via the manifest's
    ``last_update_check`` stamp, so back-to-back launches make no network calls. A record
    whose directories the user deleted is forgotten rather than re-downloaded. Best-effort:
    any failure is reported and the launch proceeds on whatever is already on disk.
    """
    try:
        now = datetime.now(UTC)
        last = last_update_check()
        if last is not None and now - last < SKILL_UPDATE_CHECK_INTERVAL:
            return
        workspace = state.get("workspace")
        if not workspace:
            return
        deleted, present = [], []
        for record in _eligible_launch_records(list_downloaded(), workspace):
            (deleted if _record_dirs_missing(record) else present).append(record)
        forget(deleted)
        if present:
            token = get_databricks_token(workspace, state.get("profile"))
            pairs = _get_updated_refs(workspace, token, present)
            updated = _update_stale_skills(workspace, token, pairs)
            if updated:
                print_success(f"Updated {updated} downloaded skill(s) from Unity Catalog.")
        set_last_update_check(now)
    except Exception as exc:  # noqa: BLE001 - a skill refresh must never block a launch
        print_note(f"Skipped checking for skill updates: {exc}")


def configure_location_skills_download_command(locations: list[str], *, path: str | None) -> int:
    """Download every skill in each schema to disk and register the skills connection.

    Downloads to ``path`` (or the home dir when None), then registers/keeps the
    schema-less MCP connection. ``skill_locations`` is never touched, so a prior
    ``--mcp`` set survives a download run. Downloading a named subset instead of whole
    schemas is a separate command (``configure_selected_skills_download_command``)."""
    state = load_state()
    workspace, profile, clients = setup_mcp_clients(state, "Skills")
    token = get_databricks_token(workspace, profile)

    download_skills_from_schema_locations(workspace, token, locations, path)

    register_schemaless_skills_connection(state, workspace, profile, clients)
    return 0


def configure_selected_skills_download_command(fqns: list[str], path: str | None) -> int:
    """Download the fully-qualified, possibly cross-schema ``fqns`` and register the connection.

    The non-interactive counterpart to the picker: downloads the named skills with
    ``download_selected_skills`` (which alone does not register), then registers/keeps
    the schema-less MCP connection, exactly as the whole-schema download does."""
    state = load_state()
    workspace, profile, clients = setup_mcp_clients(state, "Skills")
    token = get_databricks_token(workspace, profile)

    download_selected_skills(workspace, token, fqns, path)

    register_schemaless_skills_connection(state, workspace, profile, clients)
    return 0


# --- Interactive picker (selective download) --------------------------------


def _skill_download_choice(ref: SkillRef, roots: list[Path]) -> questionary.Choice:
    """Picker row for one skill: value is its FQN, title flags an on-disk bundle.

    On-disk skills stay selectable, since re-downloading is a legitimate update and
    the existing overwrite prompt confirms it. The detail footer previews the
    description behind a bold bundle-name label (the row itself shows the FQN, so the
    bundle name is the one identifier not otherwise on screen).
    """
    on_disk = " (on disk)" if existing_skill_on_disk(roots, ref.bundle_name) else ""
    description = f"{ref.bundle_name}: {ref.description}" if ref.description else None
    return questionary.Choice(title=f"{ref.fqn}{on_disk}", value=ref.fqn, description=description)


def _skills_download_background_loader(
    workspace: str, token: str, roots: list[Path]
) -> Callable[[Callable[[list[questionary.Choice]], None]], str | None]:
    """A picker ``background_loader`` that streams the workspace-wide skill walk in as choices."""

    def loader(append: Callable[[list[questionary.Choice]], None]) -> str | None:
        def on_skills(refs: list[SkillRef]) -> None:
            append([_skill_download_choice(ref, roots) for ref in refs])

        found, reason = list_all_skills(workspace, token, on_skills=on_skills)
        if reason == _SKILLS_WALK_TIMEOUT_REASON:
            return f"⚠ Timed out after {int(_SKILLS_WALK_DEADLINE_SECONDS)}s, found {len(found)} skills"
        return None

    return loader


def prompt_for_skill_download_choices(
    roots: list[Path],
    background_loader: Callable[[Callable[[list[questionary.Choice]], None]], str | None],
) -> list[str] | None:
    """Show the skill-download picker, returning the selected FQNs or None on Ctrl-C."""
    selection = scrolling_checkbox(
        "Skills:",
        choices=[],
        instruction="(space to toggle, ctrl-a all, enter to save, type to filter)",
        style=picker_style(),
        background_loader=background_loader,
        loading_noun="skills",
        show_description=True,
    ).ask()
    if selection is None:
        return None
    return [str(value) for value in selection]


def configure_skills_download_picker_command(path: str | None = None) -> int:
    """Pick skills from an interactive workspace-wide list, download them, and register.

    Opens the picker immediately and streams skills in as discovery finds them.
    Ctrl-C downloads nothing and leaves the connection untouched.
    """
    state = load_state()
    workspace, profile, clients = setup_mcp_clients(state, "Skills")
    token = get_databricks_token(workspace, profile)
    roots = skill_dir_roots(path)

    loader = _skills_download_background_loader(workspace, token, roots)
    fqns = prompt_for_skill_download_choices(roots, loader)
    if fqns is None:
        return 0

    download_selected_skills(workspace, token, fqns, path)
    register_schemaless_skills_connection(state, workspace, profile, clients)
    return 0


# --- Removing and listing downloaded skills ---------------------------------


def _record_dirs_missing(record: dict) -> bool:
    """Whether any of a record's on-disk directories no longer exists."""
    return any(not Path(directory).exists() for directory in record.get("dirs") or [])


def _download_label(record: dict) -> str:
    label = f"{record.get('fqn')}  ({record.get('scope')}: {record.get('base')})"
    return f"{label}  (missing)" if _record_dirs_missing(record) else label


def _removal_choice(record: dict, index: int) -> questionary.Choice:
    """Picker row for one downloaded skill, labeled by its scope and base (and missing dirs)."""
    return questionary.Choice(title=_download_label(record), value=index)


def _prompt_for_downloaded_skill_removal(records: list[dict]) -> list[dict] | None:
    """Checklist of downloaded skills to remove, across every base.

    Returns the selected records, ``None`` if cancelled (Ctrl-C), or ``[]`` if nothing
    is checked. Only recorded downloads are offered, so a user-authored skill directory
    with no attribution can never be selected.
    """
    if not records:
        print_note("No downloaded skills to remove.")
        return []
    choices = [_removal_choice(record, index) for index, record in enumerate(records)]
    selection = scrolling_checkbox(
        "Remove downloaded skills:",
        choices=choices,
        style=picker_style(),
        instruction="(space to toggle, ctrl-a all, enter to remove, type to filter)",
    ).ask()
    if selection is None:
        return None
    return [records[int(index)] for index in selection]


def _personal_records(records: list[dict]) -> tuple[list[dict], int]:
    """Split off managed installs: return ``(developer-owned records, managed count dropped)``.

    Managed skills are owned by the workspace config and reconciled by ``ug configure``, so
    ``ug skills remove`` never deletes one (a developer can adopt one first by re-downloading it
    with ``ug skills add``, which transfers it to their own attribution)."""
    personal = [record for record in records if record.get("scope") != "managed"]
    return personal, len(records) - len(personal)


def _note_managed_skipped(managed_count: int) -> None:
    if managed_count:
        print_note(
            f"Left {managed_count} workspace-managed skill(s) in place; those are configured by "
            "your workspace, not removable here."
        )


def remove_downloaded_skills_command(
    locations: list[str], fqns: list[str] | None = None, *, path: str | None
) -> int:
    """`ug skills remove` (download side): delete downloaded skills and forget them.

    With ``fqns``, removes those fully-qualified skills; with ``locations``, every skill
    downloaded from those ``<catalog>.<schema>`` schemas; with neither, opens a picker over
    every downloaded skill. ``path`` limits any of these to one download base. Removal is
    driven entirely by attribution, so a same-named skill the user authored is never touched,
    and workspace-managed skills are left in place for ``ug configure`` to reconcile.
    """
    if fqns is not None:
        records, managed = _personal_records(records_for_fqns(set(fqns), path))
        if not records:
            scope = f" under `{path}`" if path else ""
            joined = ", ".join(f"`{fqn}`" for fqn in fqns) or "those names"
            print_note(f"No developer-downloaded skills matching {joined}{scope}.")
            _note_managed_skipped(managed)
            return 0
    elif locations:
        records, managed = _personal_records(
            [record for location in locations for record in records_for_schema(location, path)]
        )
        if not records:
            scope = f" under `{path}`" if path else ""
            joined = ", ".join(f"`{location}`" for location in locations)
            print_note(f"No developer-downloaded skills from {joined}{scope}.")
            _note_managed_skipped(managed)
            return 0
    else:
        offered, _ = _personal_records(list_downloaded())
        selected = _prompt_for_downloaded_skill_removal(offered)
        if selected is None:
            return 0
        if not selected:
            print_note("No skills selected.")
            return 0
        records = selected

    remove_downloads(records)
    print_success(f"Removed {len(records)} downloaded skill(s).")
    return 0
