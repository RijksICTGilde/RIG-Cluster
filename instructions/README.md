# instructions/

How to work **in** this codebase: the contracts you must hold to, and the steps to add
something without hunting through the code first. Written for whoever picks up a task
here, agent or human.

Every document answers "how do I do X, and what will bite me", not "why did we build it
this way". Keep them short enough to read before starting, and correct enough to trust:
point at real files and symbols, and delete a claim rather than let it rot.

| Document | Covers |
|---|---|
| [services.md](services.md) | The service system: what a service owns, how config, forms, provisioning, manifests and approvals hook in, and how to add one |

## Which folder for what

The docs grew organically; this is the split we are holding from here on.

| Folder | Contains | Written for |
|---|---|---|
| `instructions/` | How to work in the code: contracts, hooks, step-by-step | Whoever changes the code |
| `features/` | What a feature is and why it exists, per feature | Whoever needs to understand a subsystem |
| `features/futures/` | Design briefs for work not built yet | Whoever picks up the design |
| `docs/` | Operating the platform: setup, environments, known issues, post-mortems | Whoever runs it |
| `architecture/` | System-level overviews and decision records | Whoever needs the big picture |
| `plans/` | A finished plan for one piece of work, the contract for its PR | Whoever implements that plan |

A rule of thumb when you are unsure: if it goes stale the moment the code changes, it
belongs in `instructions/` next to the contract it describes. If it explains a decision
that stays true after the code moves, it belongs in `features/` or `architecture/`.
