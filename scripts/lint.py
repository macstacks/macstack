#!/usr/bin/env python3
"""macstack lint — validate macstack.json files.

Pass 1: JSON Schema (schema/macstack.schema.json or --schema).
Pass 2: referential-integrity rules of the MACSTACK standard.

Usage:
    python3 scripts/lint.py file.macstack.json [more files...]
    python3 scripts/lint.py --schema path/or/url --categories path/or/url \
                            --coverage-areas path/or/url files...

Exit code 0 = all files pass (warnings allowed), 1 = errors found.
"""
import argparse
import json
import pathlib
import sys
import urllib.request

HIER = {"control_plane": 2, "orchestrator": 1, "worker": 0}


def load(ref: str):
    if ref.startswith(("http://", "https://")):
        with urllib.request.urlopen(ref, timeout=30) as r:
            return json.load(r)
    return json.load(open(ref))


def lint(doc: dict, categories: set | None, coverage_areas: set | None = None):
    errors, warnings = [], []
    sids = {s["id"] for s in doc.get("software", [])}
    inst = {s["id"]: {i["id"] for i in s.get("instances", [])} for s in doc.get("software", [])}
    prids = {p["id"] for p in doc.get("processes", [])}
    trids = {t["id"] for t in doc.get("triggers", [])}
    wids = {w["id"] for w in doc.get("workflows", [])}
    eids = {e["id"] for e in doc.get("entities", [])}
    rids = {r["id"] for r in doc.get("results", [])}
    gids = {g["id"] for g in doc.get("goals", [])}
    iids = {i["id"] for i in doc.get("interfaces", [])}
    mids = {m["id"] for m in doc.get("connections", {}).get("mcp", [])}
    packs = {p["name"] for p in doc.get("context", {}).get("packs", [])}
    roleids = {r["id"] for r in doc.get("roles", [])}
    stx = doc.get("stacks", {})
    xstacks = {stx.get("root", {}).get("id")} | {m["id"] for m in stx.get("substacks", [])} | {l["id"] for l in stx.get("links", [])}
    xstacks.discard(None)

    def xok(ref):
        return ref.split(":", 1)[0] in xstacks if ":" in ref else None

    if stx.get("role") == "substack" and not stx.get("root"):
        errors.append("stacks: role=substack requires root")

    used_triggers = set()
    for t in doc.get("triggers", []):
        if t.get("software") and t["software"] not in sids:
            errors.append(f"trigger {t['id']}: software unknown")
        if t.get("instance") and t["instance"] not in inst.get(t.get("software", ""), set()):
            errors.append(f"trigger {t['id']}: instance unknown")

    for r in doc.get("results", []):
        if r.get("goal") and r["goal"] not in gids:
            errors.append(f"result {r['id']}: goal unknown")
        if gids and not r.get("goal"):
            warnings.append(f"result {r['id']}: no goal while goals exist")
        for p in r.get("produced_by", []):
            if p not in prids:
                errors.append(f"result {r['id']}: produced_by '{p}' unknown")
        for f in r.get("feeds", []):
            if xok(f) is False:
                errors.append(f"result {r['id']}: feeds cross-stack '{f}' undeclared")
    for g in gids - {r.get("goal") for r in doc.get("results", [])}:
        warnings.append(f"goal {g}: no results (a goal with no path to it)")

    for p in doc.get("processes", []):
        for r in p.get("produces", []):
            if r not in rids:
                errors.append(f"process {p['id']}: produces '{r}' unknown")
        for t in p.get("tasks", []):
            if t.get("workflow") and t["workflow"] not in wids:
                errors.append(f"process {p['id']} task {t['id']}: workflow unknown")
            h = t.get("human", {})
            if h.get("role") and roleids and h["role"] not in roleids:
                errors.append(f"process {p['id']} task {t['id']}: human role unknown")

    for w in doc.get("workflows", []):
        if w.get("software") and w["software"] not in sids:
            errors.append(f"workflow {w['id']}: software unknown")
        if "trigger" in w:
            errors.append(f"workflow {w['id']}: legacy inline trigger (use the triggers collection)")
        for t in w.get("triggers", []):
            if t not in trids:
                errors.append(f"workflow {w['id']}: trigger '{t}' unknown")
            used_triggers.add(t)
        for u in w.get("uses", []):
            if xok(u) is None and u not in sids | eids:
                errors.append(f"workflow {w['id']}: uses '{u}' unknown")
            elif xok(u) is False:
                errors.append(f"workflow {w['id']}: cross-stack '{u}' undeclared")

    for e in doc.get("entities", []):
        masters = [s for s in e.get("stores", []) if s.get("role") == "master"]
        if len(masters) != 1 or masters[0]["software"] != e.get("master"):
            errors.append(f"entity {e['id']}: master must appear in stores exactly once and match")
        for s in e.get("stores", []):
            sw = s["software"]
            if xok(sw) is False:
                errors.append(f"entity {e['id']}: cross-stack store '{sw}' undeclared")
            elif xok(sw) is None and sw not in sids:
                errors.append(f"entity {e['id']}: store software '{sw}' unknown")
            if s.get("instance") and ":" not in sw and s["instance"] not in inst.get(sw, set()):
                errors.append(f"entity {e['id']}: instance '{s['instance']}' not on '{sw}'")
        for rel in e.get("relations", []):
            if rel not in eids:
                errors.append(f"entity {e['id']}: relation '{rel}' unknown")

    for i in doc.get("interfaces", []):
        if i.get("software") and i["software"] not in sids:
            errors.append(f"interface {i['id']}: software unknown")
        for ii in i.get("instances", []):
            if ii not in inst.get(i.get("software", ""), set()):
                errors.append(f"interface {i['id']}: instance '{ii}' not on its software")
        for rel in i.get("related", []):
            if rel not in iids:
                errors.append(f"interface {i['id']}: related '{rel}' unknown")
        for x in i.get("entities", []):
            if x not in eids:
                errors.append(f"interface {i['id']}: entity '{x}' unknown")
        for x in i.get("workflows", []):
            if x not in wids:
                errors.append(f"interface {i['id']}: workflow '{x}' unknown")
        for x in i.get("roles", []):
            if roleids and x not in roleids:
                errors.append(f"interface {i['id']}: role '{x}' unknown")

    for m in doc.get("connections", {}).get("mcp", []):
        if m.get("software") and m["software"] not in sids:
            errors.append(f"mcp {m['id']}: software unknown")
        if m.get("instance") and m["instance"] not in inst.get(m.get("software", ""), set()):
            errors.append(f"mcp {m['id']}: instance unknown")

    for s in doc.get("software", []):
        if categories is not None and s["category"] not in categories:
            errors.append(f"software {s['id']}: category '{s['category']}' not in the registry")
        ag = s.get("agentic", {})
        if ag:
            n = sum(1 for k in ("mcp", "api", "cli") if ag.get(k) is True)
            p2 = sum(1 for k in ("mcp", "api", "cli") if ag.get(k) == "partial")
            expected = "full" if n == 3 else "good" if n == 2 else "basic" if n == 1 else ("partial" if p2 else "none")
            if ag.get("rating") and ag["rating"] != expected:
                errors.append(f"software {s['id']}: agentic.rating '{ag['rating']}' vs computed '{expected}'")
        else:
            warnings.append(f"software {s['id']}: no agentic passport")

    ag2 = doc.get("agents", {})
    sa = ag2.get("stack_agents", [])
    said = {a["id"] for a in sa}
    hier = {a["id"]: a.get("hierarchy_role") for a in sa}

    def check_invocations(a, who):
        for inv in a.get("invocations", []):
            via = inv.get("via")
            if via == "interface" and inv.get("interface") not in iids:
                errors.append(f"{who} {a['id']}: invocation interface unknown")
            if via == "workflow" and inv.get("workflow") not in wids:
                errors.append(f"{who} {a['id']}: invocation workflow unknown")
            if via == "trigger":
                if inv.get("trigger") not in trids:
                    errors.append(f"{who} {a['id']}: invocation trigger unknown")
                else:
                    used_triggers.add(inv["trigger"])

    for a in sa:
        for acc in a.get("access", []):
            if xok(acc) is None and acc not in mids | sids | iids:
                errors.append(f"stack_agent {a['id']}: access '{acc}' unknown")
        for cp in a.get("context_packs", []):
            if packs and cp not in packs:
                errors.append(f"stack_agent {a['id']}: context pack '{cp}' unknown")
        for d in a.get("delegates_to", []):
            if d not in said:
                errors.append(f"stack_agent {a['id']}: delegates_to '{d}' unknown")
            elif hier.get(a["id"]) and hier.get(d) and HIER[hier[a["id"]]] <= HIER[hier[d]]:
                errors.append(f"stack_agent {a['id']}: delegation must go downward")
        check_invocations(a, "stack_agent")

    for a in ag2.get("managed_agents", []):
        t = a.get("tools", {})
        for m in t.get("mcp", []):
            if m not in mids:
                errors.append(f"managed_agent {a['id']}: tools.mcp '{m}' unknown")
        for w in t.get("workflows", []):
            if w not in wids:
                errors.append(f"managed_agent {a['id']}: tools.workflows '{w}' unknown")
        for ro in a.get("available_to", []):
            if roleids and ro not in roleids:
                errors.append(f"managed_agent {a['id']}: available_to '{ro}' unknown")
        check_invocations(a, "managed_agent")

    for t in trids - used_triggers:
        warnings.append(f"trigger {t}: referenced by no workflow or agent")

    # --- plugin coverage: context.plugins[].covers / scope -------------------
    # Sections a plugin can realistically teach you to build. goals/results/
    # processes/roles/integrations are authored by the architect, not taught by a
    # plugin — gap-checking them would only produce fake entries.
    TOOLED = ["software", "entities", "workflows", "triggers", "interfaces", "connections"]
    # the id-spaces of the six TOOLED sections, so a scope can narrow any of them
    elem_ids = sids | eids | wids | iids | trids | mids
    plugins = []
    for group in doc.get("context", {}).get("plugins", {}).values():
        for p in group:
            plugins.append({"id": p} if isinstance(p, str) else p)

    covered = set()
    owners = {}
    for p in plugins:
        pid = p.get("id", "?")
        for c in p.get("covers", []):
            if coverage_areas is not None and c not in coverage_areas:
                errors.append(f"plugin {pid}: covers '{c}' not in the coverage registry")
            covered.add(c)
            owners.setdefault(c, []).append(p)
        for sc in p.get("scope", []):
            if sc not in elem_ids:
                errors.append(f"plugin {pid}: scope '{sc}' resolves to no declared element")
        if not p.get("covers"):
            warnings.append(f"plugin {pid}: no covers — an agent cannot route to it")

    if plugins:
        for sec in TOOLED:
            val = doc.get(sec)
            filled = bool(val) if isinstance(val, list) else bool(val and any(val.values()))
            if filled and sec not in covered:
                n = len(val) if isinstance(val, list) else sum(len(v) for v in val.values() if isinstance(v, list))
                warnings.append(f"coverage gap: {n} {sec} and no plugin covering '{sec}'")

    for area, own in owners.items():
        if len(own) >= 2 and not any(o.get("scope") for o in own):
            ids = ", ".join(o.get("id", "?") for o in own)
            warnings.append(f"ambiguous coverage of '{area}': {ids} — narrow one with scope")

    return errors, warnings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--schema", default=str(pathlib.Path(__file__).parent.parent / "schema/macstack.schema.json"))
    ap.add_argument("--categories", default=None, help="path or URL to software-categories.json (registry)")
    ap.add_argument("--coverage-areas", default=None, help="path or URL to coverage-areas.json (registry)")
    args = ap.parse_args()

    schema = load(args.schema)
    categories = None
    if args.categories:
        categories = {c["id"] for c in load(args.categories)["categories"]}
    coverage_areas = None
    if args.coverage_areas:
        coverage_areas = {a["id"] for a in load(args.coverage_areas)["areas"]}

    try:
        import jsonschema
    except ImportError:
        print("WARNING: jsonschema not installed — schema pass skipped (pip install jsonschema)")
        jsonschema = None

    failed = False
    for f in args.files:
        doc = json.load(open(f))
        if jsonschema:
            try:
                jsonschema.validate(doc, schema)
                print(f"{f}: schema VALID")
            except jsonschema.ValidationError as e:
                print(f"{f}: SCHEMA ERROR: {e.message} at {'/'.join(map(str, e.path))}")
                failed = True
                continue
        errors, warnings = lint(doc, categories, coverage_areas)
        for e in errors:
            print(f"{f}: ERROR: {e}")
        for w in warnings:
            print(f"{f}: warning: {w}")
        if errors:
            failed = True
        else:
            print(f"{f}: OK ({len(warnings)} warnings)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
