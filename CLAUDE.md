# ha-integrations

Custom integration `gungors` (Python). No agent: the main session works here itself.

## This repository

- Each wrapper drives an original entity that is hidden in HA; keep that contract. Options,
  services and attributes are documented in the README: ha-configs and the floor card depend on it.
- Home Assistant conventions: async only, no blocking I/O in the event loop, entity platforms,
  restore state where the user expects it.
- Test before a release: unit tests with `pytest-homeassistant-custom-component` where possible.
- Release: bump `manifest.json` version, commit, GitHub release `vX.Y.Z`, update in HACS
  (`ha_manage_hacs`), restart only with Sercan's approval, then check `ha_get_logs` and the
  wrapper entities.
- A YAML-only change needs no release: change ha-configs `packages/covers.yaml` / `heating.yaml`,
  then `gungors.reload`.

## Rules (all Gungor HA repositories)

- Reply to Sercan in Turkish. Commit messages, code comments and repo docs in English.
- Work on a branch, open a PR to the default branch and merge it yourself when it is green and
  conflict-free; no PRs stacked on unmerged branches. Live changes (HA, renders) follow the repo's rules.
- Blender only through the Blender MCP (Sercan's live Blender). Home Assistant only through the
  Home Assistant MCP. Nobody reads or writes `secrets.yaml`; ask Sercan for new secrets.
- Read a repo's README first, then only the doc section you need (`grep -n '^## '`).

## Agents

Each agent lives in the repository it owns (`.claude/agents/`). Clone the repositories side by side
(`../ha-floorplan`, `../ha-configs`, ...); in a cloud session the project repositories already are.
ha-configs (with the ha-dashboards cards) and ha-integrations have no agent: the main session works
there itself, following that repository's CLAUDE.md.

| Agent | Repo | Does | Never |
|---|---|---|---|
| `modeler` | ha-floorplan | Draws the house in Blender: geometry, furniture, where things go; perspective screenshots | Render cameras, renders, deploys, HA |
| `baking` | ha-floorplan | Ortho render cameras, render layers, light effects, the page and UI, renders, deploy | Draws or moves things in the house |
| `pyscript` | ha-configs | pyscript automations in `pyscript/` (placeholder, scope still being defined) | Touches Blender |

- Small work (size, colour, one YAML line, a one-file fix): one session, no agents. Big work
  (something new from scratch, a page/card protocol change): the agent chain. No separate
  verifier: each agent checks its own work before its handoff note. No screenshots or previews
  unless a step needs one or Sercan asks.
- The main session calls the agents in order and passes each handoff note on; agents do not call
  each other. Independent steps may run in parallel (modeler draws while baking renders).
- Blender has no owner: every agent uses it for its own function and never does another agent's.
  The live Blender answers one call at a time, so calls stay short; renders and builds run in a
  background Blender (ha-floorplan `src/render/bake.py`) and never block it.
- Flows: new thing on the 3D view: main session in ha-configs (old 2D position, HA entity) ->
  modeler -> baking -> main session (`floorplan_3d.yaml` mapping). Page/card protocol change:
  baking (page + `docs/page.md`) -> main session (card in ha-dashboards). Integration change:
  ha-integrations (release) -> ha-configs (YAML, `gungors.reload`).
- Contracts (change one side, update the other): Blender object names (modeler -> baking), page
  entity ids and `docs/page.md` (baking -> card), HA entity ids (ha-configs -> all), `gungors`
  options and attributes in the ha-integrations README (ha-integrations -> ha-configs, baking).

Every agent ends with a handoff note:

```
done: <what changed, files, commits/branch>
verified: <how>
next: <agent or main session> - <what it needs to do, ids/names it needs>
open: <anything Sercan must decide>
```
