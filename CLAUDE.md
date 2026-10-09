# ha-integrations

Custom integration `gungors` (Python). Agent here: `integration`.

## This repository

- A release that needs a Home Assistant restart: restart only with Sercan's approval.

## Rules (all Gungor HA repositories)

- Reply to Sercan in Turkish. Commit messages, code comments and repo docs in English.
- Work on a branch, open a PR to the default branch and merge it yourself when it is green and
  conflict-free; no PRs stacked on unmerged branches. Live changes (HA, renders) follow the agent's rules.
- Blender only through the Blender MCP (Sercan's live Blender). Home Assistant only through the
  Home Assistant MCP. Nobody reads or writes `secrets.yaml`; ask Sercan for new secrets.
- Read a repo's README first, then only the doc section you need (`grep -n '^## '`).

## Agents

Each agent lives in the repository it owns (`.claude/agents/`). Clone the repositories side by side
(`../ha-floorplan`, `../ha-configs`, ...); in a cloud session the project repositories already are.

| Agent | Repo | Does | Never |
|---|---|---|---|
| `blender` | ha-floorplan | Edits the Blender scene and its scripts | Renders, deploys, touches HA |
| `baking` | ha-floorplan | Renders floors, builds the page, deploys renders to HA | Edits the scene |
| `qa` | ha-floorplan | Verifies the result, reports pass/fail | Changes anything |
| `config` | ha-configs | HA YAML, entities, dashboards, all cards in ha-dashboards | Touches Blender |
| `integration` | ha-integrations | Python of the `gungors` integration, tests, releases | Edits ha-configs |

- Small work (size, colour, one YAML line, a one-file fix): one session, no agents. Big work
  (something new from scratch, a page/card protocol change, integration development): the agent
  chain, ending with qa. No screenshots or previews unless a step needs one or Sercan asks.
- The main session calls the agents in order and passes each handoff note on; agents do not call
  each other. Blender runs one call at a time: never two Blender jobs at once.
- Flows: new thing on the 3D view: config (old 2D position, HA entity) -> blender -> baking -> config
  (`floorplan_3d.yaml` mapping) -> qa. Page/card protocol change: baking (page + `docs/page.md`) ->
  config (card) -> qa. Integration change: integration -> config (YAML, `gungors.reload`) -> qa.
- Contracts (change one side, update the other): Blender object names (blender -> baking), page
  entity ids and `docs/page.md` (baking -> config), HA entity ids (config -> all), `gungors`
  options and attributes in the ha-integrations README (integration -> config, baking).

Every agent ends with a handoff note:

```
done: <what changed, files, commits/branch>
verified: <how>
next: <agent> - <what it needs to do, ids/names it needs>
open: <anything Sercan must decide>
```
