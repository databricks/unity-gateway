# Ad hoc Claude provider compatibility check

This temporary branch runs the existing real Cowork UI assertions on Windows and
Intel macOS against the Claude-serving services discovered in the requested
eng-ml-inference-team-us-east-1 workspace. It does not add required CI coverage.

Each service gets a fresh runner and one model from its live Anthropic catalog,
preferring Haiku. Setup uses UG's production Desktop profile writer and credential
helper with a single-model test profile. It does not exercise the interactive
`ug configure` provider picker, change provider services, or change permissions.
The existing CI workspace bearer and subscription OAuth secret authenticate it.

`direct-api.json` records a separate short Messages request with the same provider,
model and credentials. `configuration.json` records setup/discovery failures;
`result.json` (macOS) or `native-launch.json` (Windows) records actual desktop
assertions. A direct API pass alone is not a desktop pass. Each prompt asks for
48213 without spelling those digits in the input and prohibits tools. Tool/file
operations and all-model coverage are outside this check.
