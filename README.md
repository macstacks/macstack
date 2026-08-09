# macstack.json — the MACSTACK standard

`macstack.json` is the standardized JSON file of the **MACSTACK** framework
(Multi-Agent Composable Stacks). It lives in the **root of a Claude project** and is,
in one file:

1. the **business spec** — goals and measurable results the stack must produce;
2. the **technical spec** — software, entities (with a single master data source),
   interfaces, triggers, workflows, connections, agents;
3. the **meta-config** — the source from which the project's working files are
   scaffolded (list `nextjs` in `software` → a Next.js skeleton gets generated).

The file reads **result-first**, top to bottom: goals → results → processes →
triggers → workflows → software → entities → interfaces → connections → agents →
context → resources. A process with no result is "coding for coding's sake" — the
linter rejects it.

## This repository

| Path | What it is |
|---|---|
| [`schema/macstack.schema.json`](schema/macstack.schema.json) | The JSON Schema (draft 2020-12) — the single source of truth for structure and enums |
| [`examples/`](examples/) | Complete example files: an organization root workspace, an application substack, a headless agents stack, a client BPMS |
| [`scripts/lint.py`](scripts/lint.py) | The reference linter: schema pass + referential-integrity pass |

Reusable building blocks (software passports, entity templates, trigger presets,
agent presets, the category registry) live in
[**macstacks/registry**](https://github.com/macstacks/registry).

## Use the schema in your editor

Put this as the first key of your `macstack.json` and VS Code/Cursor will
autocomplete and validate as you type:

```json
{
  "$schema": "https://raw.githubusercontent.com/macstacks/macstack/main/schema/macstack.schema.json"
}
```

## Validate a file

```bash
pip install jsonschema
python3 scripts/lint.py path/to/macstack.json \
  --categories https://raw.githubusercontent.com/macstacks/registry/main/software-categories.json
```

## Key concepts (30 seconds)

- **prototype** — a parent `macstack.json` (a GitHub repo `github:owner/repo` or a
  local absolute path). The child extends/overrides it; arrays merge by `id`.
- **stacks** — organization composition: one `root` stack (the org workspace holding
  the `substacks[]` registry) + substacks. Cross-stack references use
  `<stack-id>:<element-id>` (e.g. an entity mastered in another stack).
- **software[]** — every piece of software with a mandatory `category` (registry)
  and `type` (ready_made | constructor | framework | library | custom), strict
  layers (data | logic | interface | infrastructure), `instances[]` with URLs, and
  an Agentic-IT-Ready passport (mcp/api/cli → rating).
- **entities[]** — every entity declares all its stores and exactly **one master**
  data source; external client systems are software with `hosting: "external"`.
- **triggers[]** — a separate collection; workflows and agents reference triggers
  by id, settings live in the trigger's `config`.
- **agents** — `stack_agents` (orchestrate the whole stack, read `.claude/`, may
  modify the stack) and `managed_agents` (model + instructions + tools; invoked via
  interface / workflow / trigger / api).
- **Secrets are names only** — `resources.accesses[]` lists env key names with a
  `required` flag; values live in a secrets manager (Infisical).

## Tooling

The [`macstack-dev`](https://github.com/Agents-Store/claude-plugins/tree/main/plugins/macstack-dev)
Claude plugin (Agents Store) creates macstack.json in existing projects, generates
stacks from scratch result-first, discovers context plugins and prototypes, and
scaffolds project files in the mandatory **prototype → stack plugins → dev plugins**
order.

## Versioning

The file format version is the `"macstack": "1.0"` field. Schema releases are tagged
in this repo (`v1.x.y`). Pin a tag in automation for reproducibility; `main` tracks
the latest.
