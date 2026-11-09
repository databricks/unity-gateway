# Windows Cowork capability experiment

The branch-only `cowork-capabilities-probe.yml` workflow runs three isolated Windows
jobs. Each uses the pinned installer, public UG configure path, and existing native
inference baseline before attempting a new task through UI Automation.

- File: select a fresh folder through the native picker and require the contents of
  an input file whose random marker is absent from the prompt.
- MCP: add `system.ai.web_search` through UG, then require a completed answer and
  native evidence of a successful web-search tool call.
- Skill: download `ug_e2e.skills.fixture-summary` through UG and require the marker
  held only in its reference file in the completed response.

Setup failures, unsupported runtime capabilities, UI-driver failures, and failed
assertions remain failures. Host virtualization information is diagnostic only.
The experiment does not change the blocking integration workflow, manufacture UG
state, or modify OS virtualization settings. Uploaded evidence uses the existing
redactor and excludes profiles and credential files.
