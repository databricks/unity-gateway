"""Manual integration check; creates/updates only the specified isolated POC skill."""

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from databricks.sdk import WorkspaceClient

from publish import load_bundles, publish_bundle, remote_files

parser = argparse.ArgumentParser()
parser.add_argument("--profile", help="Optional local profile; CI uses unified auth")
parser.add_argument("--schema", required=True)
parser.add_argument("--report", required=True)
args = parser.parse_args()
w = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()
target = f"{args.schema}.publisher_roundtrip"
base = "/Skills/" + target.replace(".", "/")
results = []
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    folder = root / ".claude/skills/custom-folder"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: roundtrip-guide\ndescription: POC integration test instructions.\n---\nVersion one\n"
    )
    (folder / "references").mkdir()
    (folder / "references/obsolete.txt").write_text("Remove on next publish")
    (folder / "asset.bin").write_bytes(bytes(range(256)))
    (root / "skills.yaml").write_text(
        f"version: 1\nskills:\n  - path: .claude/skills/custom-folder\n    target: {target}\n"
    )

    def publish(prune):
        result = publish_bundle(w, load_bundles(root, "skills.yaml")[0], prune)
        results.append(result)
        print(result["status"], flush=True)
        return result

    first = publish(True)
    assert first["status"] in {"created", "updated", "unchanged"}, first
    skill_before = w.ai_gateway.get_skill(name=f"skills/{target}")
    grants_before = w.grants.get(securable_type="skill", full_name=target).as_dict()
    assert skill_before.bundle_name == "roundtrip-guide"
    assert publish(True)["status"] == "unchanged"
    (folder / "SKILL.md").write_text(
        (folder / "SKILL.md").read_text().replace("Version one", "Version two")
    )
    (folder / "references/obsolete.txt").unlink()
    (folder / "references/new.txt").write_text("Current reference")
    assert publish(False)["status"] == "failed"
    updated = publish(True)
    assert (
        updated["status"] == "updated"
        and updated["uploaded"] == 2
        and updated["removed"] == 1
    ), updated
    assert remote_files(w, base) == {"SKILL.md", "asset.bin", "references/new.txt"}
    for relative, content in load_bundles(root, "skills.yaml")[0].files.items():
        with w.files.download(f"{base}/{relative}").contents as stream:
            assert stream.read() == content
    skill_after = w.ai_gateway.get_skill(name=f"skills/{target}")
    assert skill_before.effective_owner == skill_after.effective_owner
    assert (
        grants_before
        == w.grants.get(securable_type="skill", full_name=target).as_dict()
    )
    assert publish(True)["status"] == "unchanged"
report = {
    "results": results,
    "ownership_preserved": True,
    "grants_preserved": True,
    "remote_bytes_verified": True,
    "custom_target_verified": True,
}
path = Path(args.report)
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, indent=2) + "\n")
print("All roundtrip assertions passed")
