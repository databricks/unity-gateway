# Windows Claude Desktop probe

Exploratory CI checks for AIGTWY-5412. Run only on a disposable Windows runner.

The capability probe established an accessible desktop and successful nested
Hyper-V startup. The app probe next installs an Anthropic-signed, SHA256-pinned
MSIX, launches the installed executable with Playwright, and looks for Cowork.
It fails if installation, attachment, or the Cowork control is unavailable.
Opening Cowork alone does not establish its execution environment or inference.

The user approved direct official Anthropic installer and runtime downloads for
this disposable probe. By default the workflow downloads the official 2.42304.2
x64 MSIX and verifies its SHA256 and Anthropic signature before installation.
The versioned URL and SHA256 come from Microsoft's winget-pkgs catalog:
`manifests/a/Anthropic/Claude/2.42304.2/Anthropic.Claude.installer.yaml`.
An internal installer URL and independently pinned SHA256 can also be supplied.

Playwright Core 1.62.1 is pinned through the internal JFrog npm registry, with its integrity
digest in package-lock.json. It is Microsoft's Apache-2.0-licensed package and
does not download a browser: the installed Claude executable provides Electron.
The job exchanges its GitHub OIDC identity using the same `github-actions` JFrog
provider as the existing Windows terminal-bench workflow. Temporary npm
credentials are removed after installation. The general npm proxy timed out
from this Windows runner.

Artifacts include installer version/signature/digest, attachment results, and
screenshots and visible text from the fresh app. The job receives no inference
credentials. It does not archive the app profile. Follow-up work must configure
UG with the existing CI identity, verify Cowork runtime readiness, and assert an
inference through a system.ai Claude model.
