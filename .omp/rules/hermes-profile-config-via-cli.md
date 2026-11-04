---
name: hermes-profile-config-via-cli
description: "Never hand-splice a live Hermes profile config to change plugin or engine state — use the host CLI, which owns the invariants"
condition: ["config\\.yaml[\\s\\S]{0,400}?\\.write_text\\(", "config\\.yaml[\\s\\S]{0,200}?\\bcp\\s+\"?\\$"]
scope: "tool"
---

Live Hermes profile state (`/var/lib/hermes/.hermes/profiles/<p>/config.yaml`) is owned by the host CLI, not by ad-hoc scripts.

Use the sanctioned path:

```
HERMES_HOME=/var/lib/hermes/.hermes hermes --profile <p> plugins enable <name>
HERMES_HOME=/var/lib/hermes/.hermes hermes --profile <p> plugins list
```

If that command fails, the failure is a finding, not an obstacle to route around. Diagnose the refusing layer (dependency sync, packaging, admission) and fix *that* — as with the missing `uv.lock`, which was the real cause of a refused enable. Do not conclude "the gate is broken, so bypass it" on the strength of one error message, and never record the bypass as though it were equivalent to the supported operation.

Hand-splicing YAML is how a working config gets corrupted: a text-replace across a list block produced invalid YAML in this conversation and broke the live profile until it was restored from a backup. If a config edit is genuinely unavoidable, take a backup first, edit through a YAML parser rather than string surgery, and verify by parsing plus reading the file back through the CLI — never by diff alone.

Report a bypass explicitly: say which invariant was skipped, what evidence justified it, and what the proper fix is. A silent end-run around the tool that enforces an invariant is indistinguishable from breaking that invariant later.