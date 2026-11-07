# docs/development/

How to work **in** this codebase: the contracts you must hold to, and the steps to add
something without hunting through the code first. Written for whoever picks up a task
here, agent or human.

Every document answers "how do I do X, and what will bite me", not "why did we build it
this way". Keep them short enough to read before starting, and correct enough to trust:
point at real files and symbols, and delete a claim rather than let it rot.

| Document | Covers |
|---|---|
| [services.md](services.md) | The service system: what a service owns, how config, forms, provisioning, manifests and approvals hook in, and how to add one |
| [wizard-tests.md](wizard-tests.md) | De vijf testniveaus voor een dienstconfig of een wizardflow, met een sjabloon per niveau |
| [uitloggen-in-een-applicatie.md](uitloggen-in-een-applicatie.md) | Het uitlogcontract: wis je eigen sessie voor je naar het end-session endpoint stuurt, want de keten eindigt bij BZK en komt niet terug |
| [toetsen.md](toetsen.md) | De vier lagen van de toetsen, de live groep per afhankelijkheid, en waar een nieuwe actie zijn toets en zijn regel op de kaart krijgt |
| [sops-sleutel-in-het-cluster.md](sops-sleutel-in-het-cluster.md) | `sops-age-key`: een naam in veel namespaces met per namespace een andere inhoud, en wat dat betekent als je de platformsleutel vervangt |
| [SECURITY.md](../../SECURITY.md) | Het beveiligingsbeleid: hoe je een kwetsbaarheid meldt en wat je dan van ons mag verwachten |

## Which folder for what

The docs grew organically; this is the split we are holding from here on.

| Folder | Contains | Written for |
|---|---|---|
| `docs/development/` | How to work in the code: contracts, hooks, step-by-step | Whoever changes the code |
| `docs/instructions/` | Short instructions for a single file or artefact, such as a generated file that lives in git: what it is, how it is made, what to do when it is stale | Whoever (agent or human) runs into that file |
| `features/` | What a built feature is and why it exists, per feature; `features/README.md` is the index | Whoever needs to understand a subsystem |
| `docs/` (the rest) | Operating the platform: `sandbox/`, `operations/`, `keycloak/`, `security/`, `post-mortems/` | Whoever runs it |
| `architecture/` | System-level overviews and decision records | Whoever needs the big picture |
| `references/` | Reference material that is not about the code, such as billing queries | Whoever needs the numbers |

Plans (the contract for one piece of work) and design briefs for work not built yet are not in this repo: plans live in `plans/` in RIG-Cluster-Legacy for now, and an unbuilt design becomes a card on the ZAD project board.

A rule of thumb when you are unsure: if it goes stale the moment the code changes, it
belongs in `docs/development/` next to the contract it describes. If it explains a decision
that stays true after the code moves, it belongs in `features/` or `architecture/`.
