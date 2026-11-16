# Windows MDM provisioning E2E

This test mutates machine settings. Run it only on an elevated, disposable Windows x64 host.
The CI job uses a fresh `windows-latest` runner. It refuses pre-existing Unity Gateway install or
managed-policy directories and requires `UG_MDM_E2E=1`.

## Scenario and expected evidence

1. Create a fresh local standard user. Grant only batch-logon rights so Task Scheduler can run it
   without a password prompt; never add it to Administrators. First force a short worker timeout
   and require its real grandchild process to be terminated as well.
2. Run the real `scripts/mdm/windows-provision.ps1` as **SYSTEM**, passing the checked-out commit
   through its public `-UgRef` parameter. No fake binaries, config stubs, or generated ug state.
3. Require the shared npm agent installs, workspace policy, `mdm_os=Windows`, Claude managed
   settings/header plugin, and Codex requirements. Check SYSTEM/Administrators ownership and deny
   broad user write ACLs. The new user's application state must still be absent.
4. Open fresh Windows PowerShell processes as the standard user, loading the real all-users
   profiles. Run `claude -p` and `codex exec` through those functions. Require a correct completed
   `42` answer on stdout, zero exit status, shared `.cmd` resolution, and no leaked
   `UG_SKIP_OS_SETTINGS` in the caller.
5. Compare the full shared install and managed-policy trees before/after launches: file hashes,
   sizes, timestamps, attributes, ACLs and junction targets must not change. Junctions are recorded
   without traversing them; managed Python's patch directory is checked separately within its root.
   Require user-owned per-user state and no per-user managed-policy cache.
6. Scan managed files, the deployment marker/script and SYSTEM's ug/agent/Databricks state for the
   provisioning secret and JWT-shaped tokens. Reject agent installs in SYSTEM's profile npm.

The inference bearer is minted separately with `ai-gateway unity-catalog`, standing in for the
user's browser sign-in; the script itself still requests its real read-only provisioning token.
The SP secret never reaches the standard-user task. Authentication through a browser, interactive
agent TUIs, ARM64, Intune package delivery, reruns, uninstall, and network/OS restart recovery are
not covered by this test. A headless inference pass does not establish those outcomes.

## Run

Use the same dedicated admin workspace credentials as the Mac/Linux MDM E2E:
`E2E_ADMIN_WORKSPACE`, `E2E_ADMIN_SP_CLIENT_ID`, `E2E_ADMIN_SP_CLIENT_SECRET` in GitHub. The
workspace must publish both Claude Code and Codex with usable models and grant the SP access to
them. No developer's Databricks profile is selected.

For an explicitly authorized disposable local VM, set the following in an elevated Windows
PowerShell session, obtaining the secret from your approved source rather than a literal command:

```powershell
$env:UG_MDM_E2E = '1'
$env:UG_MDM_E2E_WORKSPACE = 'https://<workspace>.cloud.databricks.com'
$credential = Get-Credential -UserName '<client-id>' -Message 'MDM E2E provisioning account'
$env:UG_MDM_E2E_CLIENT_ID = $credential.UserName
$env:UG_MDM_E2E_CLIENT_SECRET = $credential.GetNetworkCredential().Password
try {
  $ref = git rev-parse HEAD
  if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve checkout.' }
  ./tests/e2e_mdm_windows/run.ps1 -UgRef $ref
} finally {
  Remove-Item Env:UG_MDM_E2E_CLIENT_SECRET
  $credential = $null
}
```

The full commit must be fetchable from `databricks/unity-gateway`: the provisioning script installs
that commit from GitHub. Uncommitted changes are not tested by the installed ug. The PowerShell
provisioning script itself is copied byte-for-byte from the checkout. As with the Mac/Linux lane,
the script selects its npm agent versions; this job does not claim to test workflow version
input overrides. The integration gate allows this lane to be skipped only on those pinned runs;
failures, cancellations and unexpected skips block it. Fork PRs do not receive credentials.

## Credentials, timeouts and cleanup

Task actions contain only the worker path and a random pipe name. A named pipe restricted to
SYSTEM/Administrators carries provisioning inputs; a separate pipe also admits the standard user
for that user's tasks. The controller checks the kernel-reported client PID and its actual SID
before sending a request. Neutral input environment variables are removed before calling the
script's named parameters, so raw SP credentials cannot select Databricks unified auth in npm/ug
children. The user task receives only its inference bearer.

The worker clears the inherited process environment, supplies the selected OS/network settings,
closes stdin, captures stdout/stderr separately and bounds execution. It assigns itself to a
kill-on-close Windows Job Object before spawning children. Ending or timing out a task kills its
remaining descendants, including processes whose immediate parent has exited. Output is scrubbed
for known credentials and JWT-shaped tokens; raw output, environments, user homes and secret-bearing
request data are not uploaded as artifacts. The controller logs installed tool versions and removes
its tasks, test user/profile, batch-logon right and staged code. Provisioned machine files are left
on the disposable host for the runner teardown; this is not a cleanup script for a developer laptop.

PowerShell transcription or script-block logging configured outside this harness can record runtime
data; use only the disposable test environment and its approved test credentials. This test does
not assert that all operating-system auditing is secret-free.

## Offline checks

```powershell
./tests/mdm/windows-e2e.Tests.ps1
./tests/mdm/windows-provision.Tests.ps1
```

```bash
uv run pytest tests/test_windows_mdm_e2e_contract.py tests/test_integration_contract.py -q
```

These cover syntax/native declaration compilation, redaction, scan/snapshot boundaries, pipe EOF
handling, profile/environment restoration and CI guards. They do not run SYSTEM tasks or inference
and are not evidence of a Windows end-to-end pass.
