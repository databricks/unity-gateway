from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from databricks.sdk.errors import NotFound

from publish import load_bundles, publish_bundle, remote_files


def bundle(tmp_path, mapping=None):
    folder = tmp_path / "skills" / "guide"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: guide\ndescription: Test instructions.\n---\nContent\n"
    )
    (folder / "reference.txt").write_text("Reference")
    (tmp_path / "skills.yaml").write_text(
        mapping
        or "version: 1\nskills:\n  - path: skills/guide\n    target: acme.skills.guide\n"
    )
    return load_bundles(tmp_path, "skills.yaml")[0]


def client(files=None, finalized=True):
    w = MagicMock()
    files = files or {}
    w.ai_gateway.get_skill.return_value = SimpleNamespace(
        finalize_time=123 if finalized else None
    )
    w.files.list_directory_contents.return_value = [
        SimpleNamespace(path="/Skills/acme/skills/guide/" + p, is_directory=False)
        for p in files
    ]
    w.files.download.side_effect = lambda path: SimpleNamespace(
        contents=BytesIO(files[path.removeprefix("/Skills/acme/skills/guide/")])
    )
    return w


def test_create_upload_finalize(tmp_path):
    b = bundle(tmp_path)
    w = client()
    w.ai_gateway.get_skill.side_effect = NotFound("missing")
    result = publish_bundle(w, b, False)
    assert result["status"] == "created"
    assert result["uploaded"] == 2
    w.ai_gateway.create_skill.assert_called_once()
    assert w.files.upload.call_args_list[-1].args[0].endswith("/SKILL.md")
    w.ai_gateway.finalize_skill.assert_called_once_with(name="skills/acme.skills.guide")


def test_idempotent_rerun(tmp_path):
    b = bundle(tmp_path)
    w = client(b.files)
    assert publish_bundle(w, b, True)["status"] == "unchanged"
    w.files.upload.assert_not_called()
    w.ai_gateway.finalize_skill.assert_not_called()


def test_stale_files_require_explicit_prune(tmp_path):
    b = bundle(tmp_path)
    w = client({**b.files, "old.txt": b"old"})
    assert publish_bundle(w, b, False)["status"] == "failed"
    w.files.upload.assert_not_called()
    w.files.delete.assert_not_called()
    result = publish_bundle(w, b, True)
    assert result["status"] == "updated" and result["removed"] == 1
    w.files.delete.assert_called_once_with("/Skills/acme/skills/guide/old.txt")
    w.ai_gateway.delete_skill.assert_not_called()


def test_failed_upload_does_not_finalize_or_prune(tmp_path):
    b = bundle(tmp_path)
    w = client({"old.txt": b"old"})
    w.files.upload.side_effect = RuntimeError("secret request data")
    result = publish_bundle(w, b, True)
    assert result["status"] == "failed" and result["error"] == "RuntimeError"
    w.ai_gateway.finalize_skill.assert_not_called()
    w.files.delete.assert_not_called()


def test_recover_unfinalized_identical_bundle(tmp_path):
    b = bundle(tmp_path)
    w = client(b.files, finalized=False)
    assert publish_bundle(w, b, False)["status"] == "updated"
    w.ai_gateway.finalize_skill.assert_called_once()


def test_discovery_and_custom_uc_name(tmp_path):
    b = bundle(
        tmp_path,
        "version: 1\ndirectories:\n  - path: skills\n    schema: acme.skills\n",
    )
    assert b.target == "acme.skills.guide"
    (tmp_path / "skills.yaml").write_text(
        "version: 1\nskills:\n  - path: skills/guide\n    target: acme.skills.custom_name\n"
    )
    assert load_bundles(tmp_path, "skills.yaml")[0].target == "acme.skills.custom_name"


def test_directory_inputs_need_no_mapping_file(tmp_path):
    bundle(tmp_path)
    (tmp_path / "skills.yaml").unlink()
    bundles = load_bundles(
        tmp_path, skills_directory="skills", target_schema="acme.skills"
    )
    assert bundles[0].target == "acme.skills.guide"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"skills_directory": "skills"},
        {"target_schema": "acme.skills"},
        {
            "mapping_file": "skills.yaml",
            "skills_directory": "skills",
            "target_schema": "acme.skills",
        },
    ],
)
def test_directory_inputs_reject_ambiguous_configuration(tmp_path, kwargs):
    bundle(tmp_path)
    with pytest.raises(ValueError, match="together"):
        load_bundles(tmp_path, **kwargs)


def test_duplicate_target_fails_before_publish(tmp_path):
    bundle(tmp_path)
    mapping = (
        "version: 1\nskills:\n"
        + "  - path: skills/guide\n    target: acme.skills.guide\n" * 2
    )
    (tmp_path / "skills.yaml").write_text(mapping)
    with pytest.raises(ValueError, match="same UC skill"):
        load_bundles(tmp_path, "skills.yaml")


@pytest.mark.parametrize("path", ["../outside", "/tmp/outside"])
def test_outside_source_rejected(tmp_path, path):
    bundle(tmp_path)
    (tmp_path / "skills.yaml").write_text(
        f"version: 1\nskills:\n  - path: {path}\n    target: acme.skills.guide\n"
    )
    with pytest.raises(ValueError, match="escapes|relative"):
        load_bundles(tmp_path, "skills.yaml")


def test_symlink_rejected(tmp_path):
    bundle(tmp_path)
    (tmp_path / "skills/guide/link").symlink_to(tmp_path / "skills.yaml")
    with pytest.raises(ValueError, match="Symlinks"):
        load_bundles(tmp_path, "skills.yaml")


def test_all_folders_validated_before_publish(tmp_path):
    bundle(tmp_path)
    folder = tmp_path / "skills/bad"
    folder.mkdir()
    (folder / "SKILL.md").write_text("Missing frontmatter")
    (tmp_path / "skills.yaml").write_text(
        "version: 1\ndirectories:\n  - path: skills\n    schema: acme.skills\n"
    )
    with pytest.raises(ValueError, match="frontmatter"):
        load_bundles(tmp_path, "skills.yaml")


def test_recursive_remote_listing():
    w = MagicMock()
    w.files.list_directory_contents.side_effect = [
        [SimpleNamespace(path="/Skills/a/b/c/references", is_directory=True)],
        [SimpleNamespace(path="/Skills/a/b/c/references/guide.md", is_directory=False)],
    ]
    assert remote_files(w, "/Skills/a/b/c") == {"references/guide.md"}


def test_api_cannot_return_paths_outside_bundle():
    w = MagicMock()
    w.files.list_directory_contents.return_value = [
        SimpleNamespace(path="/Skills/a/b/other/file", is_directory=False)
    ]
    with pytest.raises(ValueError):
        remote_files(w, "/Skills/a/b/c")
