# Deploy Unity Gateway with MDM

Use mobile device management (MDM) to install Unity Gateway on a pilot device, apply the
workspace's coding-agent configuration, and let developers sign in with their own accounts.

## 1. Publish the workspace configuration

Enable Claude Code and Codex in your workspace's coding-agent configuration and select models
that the provisioning service principal can access. Grant it the required access to configured
MCP servers and skills as well. Use a dedicated OAuth secret with the `ai-gateway:read` and
`unity-catalog:read` scopes for provisioning, not a developer's credentials.

## 2. Choose your platform

| Platform | Provisioning script | Deployment instructions |
| --- | --- | --- |
| macOS | [macos-provision.sh](macos-provision.sh) | [Jamf Pro setup](macos-setup.md) |
| Windows | [windows-provision.ps1](windows-provision.ps1) | [Intune Win32 setup](windows-setup.md) |

For Windows, follow the numbered Intune guide in order: prepare the package, create a one-device
group, configure the app, assign it, and verify installation. Package preparation can use a
separate Windows VM from the enrolled deployment device. Do not run the installer manually
before checking Intune delivery.

## 3. Prepare the provisioning inputs

| Input | macOS | Windows |
| --- | --- | --- |
| Workspace URL | Policy parameter 4 | `-Workspace` |
| Service principal client ID | Policy parameter 5 | `-ClientId` |
| Service principal secret | Policy parameter 6 | `-ClientSecret` or process environment |
| Installation context | root | SYSTEM, 64-bit Windows PowerShell |

Intune install arguments can expose the secret through management or process logs; they are not
secure secret injection. Review the [Windows credential requirements](windows-setup.md#before-you-start)
before deployment. Do not embed credentials in the source script or package, and redact logs
before sharing them.

If your network requires PyPI or npm mirrors, configure approved registry endpoints in the
installer's process environment before packaging it. A working interactive-user network
connection does not establish that SYSTEM can download dependencies. The Windows guide has an
[optional proxy example](windows-setup.md#1-prepare-the-package).

## 4. Deploy to one device

Use the platform guide to assign the policy or app only to a pilot device. The script installs
shared dependencies, then runs `ug configure --global --mdm` to install the workspace policy and
supported agents' machine-wide settings. The shared runtime must be readable/runnable by
standard users, not installed under root's or SYSTEM's private profile.

On Windows, monitor the app's **Device install status**, not just the device Sync pane. The initial
pilot detects `C:\Program Files\UnityGateway\provisioned.json`, written after provisioning finishes.
If the marker already exists, Intune may report Installed without running the new package.

## 5. Verify as a developer

After Windows installation, restart or sign out and back in so terminals inherit the machine PATH.
On macOS, open a new terminal. Use a normal, non-administrator session:

```text
claude
```

Sign in to Databricks if prompted, complete a prompt, exit, and test Codex separately:

```text
codex
```

Confirm that requests use the expected workspace/models and that configured MCP servers and skills
work. Native agent launches do not establish the MDM wrapper behavior; follow the platform guide's
command-resolution checks too.

## 6. Expand and maintain the deployment

Expand the assignment only after the pilot succeeds. Rotate the provisioning secret after the
rollout window; developers continue to authenticate with their own OAuth.

Review the platform guide before software updates or removal. Windows marker-existence detection
does not schedule recurring configuration refreshes. `ug revert` alone does not uninstall shared
tools or restore execution policy; the Windows script's `-Uninstall` handles its deployment files
and profile blocks, while leaving shared Git and execution policy unchanged.

For which settings a managed configure run owns, see the
[admin-managed agent settings reference](../../docs/admin-managed-settings-reference.md).
