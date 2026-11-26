# Smart Router model-list validation

UG checks whether the configured router supports the session's model list before
routing tasks. Validation checks the candidate list without selecting a model,
running a task classifier, or sending a task or prompt. Task routing still uses
`routes:select`.

## When validation runs

Smart-routed `ug claude` and `ug codex` terminal launches start validation in the
background. The request has a 10-second deadline and does not block agent launch.
Claude holds its routing banner and Codex withholds its state badge while the
startup result is pending.

`/smart-router on` in Claude and `$smart-router on` in Codex validate before
turning routing on, including when routing is already on. Turning routing off
does not require validation. Validation runs automatically; `UG_ROUTE_VALIDATION`
and `UG_STARTUP_ROUTE_VALIDATION` are not required.

Omnigent launches skip validation. On POSIX, `ug codex app-server` also skips the
background startup check because launching the server replaces the UG process.

## Request

UG sends an authenticated JSON request to the configured workspace:

```http
POST /ai-gateway/routing/v1/routes:validate
Content-Type: application/json
Authorization: Bearer <workspace-token>
```

Illustrative request body:

```json
{
  "route_options": [
    {"model": "system.ai.claude-sonnet-5-5", "harness": "claude"},
    {"model": "system.ai.claude-haiku-4-5", "harness": "claude"}
  ],
  "route_selector": {"router_name": "stable"}
}
```

UG uses the session's discovered model options and configured router name.
`SMART_ROUTER_NAME` overrides the default router, `stable`. Claude options use
`claude`; Codex options use `codex`. The example model names do not guarantee
support in a particular workspace or router recipe.

For a Model Provider Service launch, UG includes the resolved
`Databricks-Model-Provider-Service` header and sends that provider's model IDs.
Mixed model sources use qualified model names without that provider header,
matching the session's routing and inference configuration.

## Response and session behavior

The endpoint returns `is_supported`. A supported list needs at least two eligible
options and must satisfy the router recipe's requirements. A list can contain
ineligible options and still support routing.

Unsupported responses can include these diagnostic fields:

| Field | Meaning |
| --- | --- |
| `metadata.routing_unsupported_reason` | Why the list cannot support routing, such as `insufficient_eligible_options` |
| `eligible_route_options` | Options the router can use |
| `ineligible_route_options` | Rejected options, with a `reason` for each rejection |
| `available_route_options` | Options available for a supported configuration |

UG handles the result as follows:

| Result | Startup and enable-command behavior |
| --- | --- |
| `is_supported: true` | Routing remains on or can be enabled |
| `is_supported: false` | Routing turns off or stays off, with an unsupported-list warning |
| HTTP 404 | Routing remains available; no validation warning or logs row |
| Other HTTP errors, network failure, timeout, or malformed response | Routing turns off or stays off, with a validation-failure warning |

An HTTP 404 does not establish that the model list is supported. It lets sessions
continue when the endpoint is unavailable or disabled for the workspace.
Validation changes session controls, not the saved workspace model configuration.
An unsupported or failed check does not prevent the agent from continuing with
routing off.

## Diagnostics

Open `/smart-router logs` in Claude or `$smart-router logs` in Codex. Unsupported
checks record the router, eligible and ineligible options, rejection reasons,
available options for the current agent, and validation duration. Failed checks
record a failure reason instead. Supported results and HTTP 404 do not add a
validation row.

For an unsupported list, compare the configured models with the available options
and ask the workspace admin to update the managed model configuration if needed.
Run `ug configure` after a managed configuration change. For a failed check,
inspect the failure reason and restore workspace connectivity or authentication
before enabling routing again.

Codex displays a yellow `ERROR` badge when validation disables routing.
See [Codex Smart Router footer](codex-smart-router-statusline.md) for warning
lifetime and footer behavior.
