#!/usr/bin/env python3
"""macstack lint — validate macstack.json files.

Pass 1: JSON Schema (schema/macstack.schema.json or --schema).
Pass 2: referential-integrity rules of the MACSTACK standard.

Usage:
    python3 scripts/lint.py file.macstack.json [more files...]
    python3 scripts/lint.py --schema path/or/url --categories path/or/url \
                            --coverage-areas path/or/url \
                            --marketplace path/or/url [--marketplace ...] \
                            --prototype parent.macstack.json [--prototype grandparent...] \
                            --root path/to/the/project files...

Every flag takes a local path or an http(s) URL except --root (a directory).
    --prototype    the file named by `prototype`, loaded and merged by id before the integrity
                   pass. Repeatable, nearest parent first (the chain). One linted file only.
                   Without it, an id that is unknown but could be inherited is a WARNING.
    --marketplace  a plugin marketplace.json, repeatable (names are unioned). Every plugin in
                   context.plugins / context.packs and every `plugin:` skill prefix must be a
                   plugins[].name in one of them.
    --root         the project root; workflows[].source must exist relative to it.

Exit code 0 = all files pass (warnings allowed), 1 = errors found, 2 = bad usage or an
unreadable input.
"""
import argparse
import json
import os
import pathlib
import re
import sys
import urllib.request

HIER = {"control_plane": 2, "orchestrator": 1, "worker": 0}

# Values that must never sit in an env-name field. Prefix patterns are anchored on the left by
# "not a letter or digit" so a NAME such as `task_live_url` is not an `sk_live_` key, while
# `STRIPE_KEY=sk_live_...` (a pasted assignment) still is.
_L = r"(?<![A-Za-z0-9])"
SECRET_PATTERNS = [
    ("Stripe secret key", re.compile(_L + r"sk_(?:live|test)_")),
    ("Slack token", re.compile(_L + r"xox[abprs]-")),
    ("GitHub token", re.compile(_L + r"gh[pousr]_")),
    ("GitHub fine-grained token", re.compile(_L + r"github_pat_")),
    ("AWS access key id", re.compile(_L + r"AKIA[0-9A-Z]{16}")),
    ("PEM private key", re.compile(r"-----BEGIN")),
]

# Sections whose items are keyed by `id` (packs by `name`) and merge by that key across a prototype chain.
MERGE_SECTIONS = ["goals", "results", "processes", "triggers", "workflows", "software",
                  "entities", "interfaces", "roles"]

# lifecycle status tokens kept valid for old files; the tracker five are canonical.
DEPRECATED_STATUS = {
    "doing": "use 'in_progress'",
    "dropped": "use 'cancelled'",
    "blocked": ("'blocked' is not a status: keep the real one ('todo' or 'in_progress') and express "
                "the blocker as a dependency (`blocked_by` on the blocking task)"),
}


def load(ref: str):
    if ref.startswith(("http://", "https://")):
        req = urllib.request.Request(ref, headers={"User-Agent": "macstack-lint"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    return json.load(open(ref))


def load_or_exit(what: str, ref: str):
    """Like load(), but an unreadable --flag input is a usage error (exit 2), not a traceback."""
    try:
        return load(ref)
    except Exception as e:  # noqa: BLE001 - any failure to read is reported the same way
        print(f"ERROR: cannot read {what} {ref}: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)


def load_marketplaces(refs):
    """Union of plugins[].name and renames across every given marketplace.json."""
    names, renames = set(), {}
    for ref in refs:
        data = load_or_exit("marketplace", ref)
        try:
            names |= {p["name"] for p in data["plugins"]}
        except (KeyError, TypeError):
            print(f"ERROR: marketplace {ref} has no plugins[].name", file=sys.stderr)
            sys.exit(2)
        renames.update(data.get("renames") or {})
    return {"names": names, "renames": renames}


# --- own-document checks (never run against a merged prototype) -------------------------------

def _dups(items, key, label, out):
    seen, said = set(), set()
    for it in items or []:
        v = it.get(key) if isinstance(it, dict) else None
        if v is None:
            continue
        if v in seen and v not in said:
            out.append(f"duplicate {label} '{v}'")
            said.add(v)
        seen.add(v)


def check_duplicates(doc):
    """Ids that collapse into a set in the integrity pass would hide a second definition."""
    out = []
    for sec in MERGE_SECTIONS:
        if sec == "software":
            _dups(doc.get(sec), "id", "software id", out)
        else:
            _dups(doc.get(sec), "id", f"{sec} id", out)
    for s in doc.get("software", []) or []:
        _dups(s.get("instances"), "id", f"instance id on software '{s.get('id')}':", out)
    for p in doc.get("processes", []) or []:
        _dups(p.get("tasks"), "id", f"task id in process '{p.get('id')}':", out)
    _dups((doc.get("connections") or {}).get("mcp"), "id", "connections.mcp id", out)
    _dups((doc.get("context") or {}).get("packs"), "name", "context.packs name", out)
    ag = doc.get("agents") or {}
    _dups(ag.get("stack_agents"), "id", "agents.stack_agents id", out)
    _dups(ag.get("managed_agents"), "id", "agents.managed_agents id", out)
    stx = doc.get("stacks") or {}
    ns = ([stx["root"]] if isinstance(stx.get("root"), dict) else []) + list(stx.get("substacks", []) or []) \
        + list(stx.get("links", []) or [])
    _dups(ns, "id", "stacks id", out)
    return out


def _walk_env(node, path, out):
    """Yield the string values of env-like fields (`env`, `*_env`) with their JSON paths."""
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else k
            if k == "env" or k.endswith("_env"):
                if isinstance(v, str):
                    out.append((p, v))
                elif isinstance(v, list):
                    out.extend((f"{p}[{i}]", x) for i, x in enumerate(v) if isinstance(x, str))
                elif isinstance(v, dict):
                    out.extend((f"{p}.{kk}", x) for kk, x in v.items() if isinstance(x, str))
            else:
                _walk_env(v, p, out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk_env(v, f"{path}[{i}]", out)


def check_secrets(doc):
    """Env fields hold NAMES. A value that looks like a credential is an error that names the
    field path and the kind of secret - never the value itself."""
    out, found = [], []
    _walk_env(doc, "", found)
    for path, value in found:
        for label, rx in SECRET_PATTERNS:
            if rx.search(value):
                out.append(f"{path}: looks like a secret ({label}); env fields hold key NAMES, never values "
                           f"(the value is not shown)")
                break
    return out


def check_sources(doc, root):
    """workflows[].source must exist relative to the project root. Absolute paths and paths that
    climb out of the root are errors on their own: `source` is a path *from the project root*."""
    out = []
    root = pathlib.Path(root)
    for w in doc.get("workflows", []) or []:
        src = w.get("source")
        if not src:
            continue
        parts = pathlib.PurePosixPath(src.replace("\\", "/")).parts
        if src.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", src) or ".." in parts:
            out.append(f"workflow {w['id']}: source '{src}' must be a path relative to the project root "
                       f"(no leading / and no ..)")
        elif not (root / src).exists():
            out.append(f"workflow {w['id']}: source '{src}' not found under the project root (--root)")
    return out


def check_deprecated_statuses(doc):
    out = []
    lc = doc.get("lifecycle") or {}
    for sec in ("next_steps", "milestones"):
        for i, it in enumerate(lc.get(sec, []) or []):
            if isinstance(it, dict) and it.get("status") in DEPRECATED_STATUS:
                tok = it["status"]
                out.append(f"lifecycle.{sec}[{i}] {it.get('id', '?')}: status '{tok}' is deprecated - "
                           f"{DEPRECATED_STATUS[tok]} (the tracker five: backlog, todo, in_progress, done, cancelled)")
    return out


def check_marketplace(doc, marketplace):
    """Every plugin and every `plugin:` skill prefix must be a name in a given marketplace."""
    names, renames = marketplace["names"], marketplace["renames"]
    out = []

    def problem(name):
        if name in names:
            return None
        if name in renames:
            new = renames[name]
            return "was removed from the marketplace" if new is None else f"was renamed to {new} in the marketplace"
        return "is not a plugin in any given marketplace"

    ctx = doc.get("context") or {}
    for group in (ctx.get("plugins") or {}).values():
        for p in group:
            pid = p if isinstance(p, str) else p.get("id", "?")
            if (why := problem(pid)):
                out.append(f"plugin {pid}: {why}")
    for pack in ctx.get("packs", []) or []:
        for pid in pack.get("plugins", []) or []:
            if (why := problem(pid)):
                out.append(f"context.packs '{pack.get('name')}': plugin '{pid}' {why}")
    for p in doc.get("processes", []) or []:
        skill = p.get("skill")
        if skill and ":" in skill and (why := problem(skill.split(":", 1)[0])):
            out.append(f"process {p['id']}: skill '{skill}' - plugin '{skill.split(':', 1)[0]}' {why}")
    return out


# --- prototype chain ---------------------------------------------------------------------------

def _is_remote(ref):
    return ref.startswith(("github:", "http://", "https://"))


def _ref_keys(ref):
    """Names a reference can be recognised by. A folder ref also stands for <folder>/macstack.json
    and <folder>/macstack/macstack.json, which is how the standard resolves a folder."""
    if not isinstance(ref, str) or not ref:
        return set()
    if _is_remote(ref):
        return {ref.split("#", 1)[0].rstrip("/").lower()}
    return {os.path.realpath(ref)}


def _file_keys(ref):
    """Identity of a loaded file: its own path, plus the folder it represents when it is the
    macstack.json of a project (legacy <repo>/macstack.json or <repo>/macstack/macstack.json)."""
    keys = _ref_keys(ref)
    if ref and not _is_remote(ref) and os.path.basename(ref) == "macstack.json":
        folder = os.path.dirname(os.path.realpath(ref))
        keys.add(folder)
        if os.path.basename(folder) == "macstack":
            keys.add(os.path.dirname(folder))
    return keys


def check_prototype_chain(doc, doc_ref, prototypes):
    """Cycles in the chain [this file, nearest prototype, ...]. Returns (errors, open_ref) where
    open_ref is the prototype reference the LAST file still names - an ancestor nobody passed."""
    chain = [(doc_ref, doc)] + list(prototypes)
    idents = [_file_keys(ref) if ref else set() for ref, _ in chain]
    label = lambda k: chain[k][0] or "this file"  # noqa: E731
    errors, open_ref = [], None
    for k, (ref, d) in enumerate(chain):
        for j in range(k):
            if idents[j] & idents[k]:
                errors.append(f"prototype cycle: {label(k)} is listed twice in the chain")
        target = d.get("prototype")
        if not target:
            continue
        hit = next((j for j in range(k + 1) if _ref_keys(target) & idents[j]), None)
        if hit is not None:
            errors.append(f"prototype cycle: {label(k)} -> {label(hit)}")
        elif k == len(chain) - 1:
            open_ref = target
    return errors, open_ref


def _overlay(acc, new, key):
    """Merge two lists of items by `key`: a new item extends/overrides the one with the same key."""
    out = list(acc)
    pos = {it.get(key): i for i, it in enumerate(out) if isinstance(it, dict)}
    for it in new or []:
        if not isinstance(it, dict):
            continue
        k = it.get(key)
        if k in pos:
            out[pos[k]] = {**out[pos[k]], **it}
        else:
            pos[k] = len(out)
            out.append(it)
    return out


def merge_prototypes(doc, prototypes):
    """The effective document: the prototype chain's id-keyed sections merged by id, the furthest
    ancestor first and this file last (it wins). `prototype_exclude` entries ('<section>.<id>')
    drop what the ancestors contributed before the excluding file adds its own items. Only the
    id-keyed sections are merged; every other key is taken from this file alone."""
    chain = [doc] + [d for _, d in prototypes]
    acc = {sec: [] for sec in MERGE_SECTIONS}
    acc.update({"connections.mcp": [], "context.packs": [], "agents.stack_agents": [], "agents.managed_agents": []})
    for d in reversed(chain):
        for ex in d.get("prototype_exclude", []) or []:
            sec, _, ident = ex.partition(".")
            if sec in acc:
                acc[sec] = [it for it in acc[sec] if it.get("id") != ident]
        for sec in MERGE_SECTIONS:
            acc[sec] = _overlay(acc[sec], d.get(sec), "id")
        acc["connections.mcp"] = _overlay(acc["connections.mcp"], (d.get("connections") or {}).get("mcp"), "id")
        acc["context.packs"] = _overlay(acc["context.packs"], (d.get("context") or {}).get("packs"), "name")
        for k in ("stack_agents", "managed_agents"):
            acc[f"agents.{k}"] = _overlay(acc[f"agents.{k}"], (d.get("agents") or {}).get(k), "id")
    merged = dict(doc)
    for sec in MERGE_SECTIONS:
        if acc[sec]:
            merged[sec] = acc[sec]
    if acc["connections.mcp"]:
        merged["connections"] = {**(doc.get("connections") or {}), "mcp": acc["connections.mcp"]}
    if acc["context.packs"]:
        merged["context"] = {**(doc.get("context") or {}), "packs": acc["context.packs"]}
    agents = {**(doc.get("agents") or {})}
    for k in ("stack_agents", "managed_agents"):
        if acc[f"agents.{k}"]:
            agents[k] = acc[f"agents.{k}"]
    if agents:
        merged["agents"] = agents
    return merged


def lint(doc: dict, categories: set | None, coverage_areas: set | None = None, *,
         prototypes: list | None = None, doc_ref: str | None = None,
         root: str | None = None, marketplace: dict | None = None):
    """Integrity pass. Returns (errors, warnings).

    prototypes   [(ref, loaded_doc), ...] nearest parent first: merged by id before the pass.
    doc_ref      path of the linted file; lets a prototype cycle back to it be recognised.
    root         project root: when given, workflows[].source must exist under it.
    marketplace  {"names": set, "renames": dict}: when given, plugin names must be in it.

    Own-document rules (duplicates, secrets, sources, deprecated statuses, plugin names) run on
    `doc` as written; the referential rules run on the effective document, i.e. after the merge.
    """
    errors, warnings = [], []
    own = doc

    errors += check_duplicates(own)
    errors += check_secrets(own)
    if root is not None:
        errors += check_sources(own, root)
    warnings += check_deprecated_statuses(own)
    if marketplace is not None:
        errors += check_marketplace(own, marketplace)

    # An id the file does not define may still come from its prototype. With the prototype merged
    # a miss is a real error; with it unmerged (or with ancestors nobody passed) it is only a
    # warning that says where to look - an error there would blame the file for the parent's ids.
    if prototypes:
        chain_errors, open_ref = check_prototype_chain(own, doc_ref, prototypes)
        errors += chain_errors
        if not own.get("prototype"):
            warnings.append("--prototype given, but this file sets no `prototype`")
        doc = merge_prototypes(own, prototypes)
    else:
        open_ref = own.get("prototype") or None

    def unk(msg):
        if open_ref:
            warnings.append(f"{msg} (may come from prototype {open_ref}; pass --prototype)")
        else:
            errors.append(msg)

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
            unk(f"trigger {t['id']}: software unknown")
        if t.get("instance") and t["instance"] not in inst.get(t.get("software", ""), set()):
            unk(f"trigger {t['id']}: instance unknown")

    for r in doc.get("results", []):
        if r.get("goal") and r["goal"] not in gids:
            unk(f"result {r['id']}: goal unknown")
        if gids and not r.get("goal"):
            warnings.append(f"result {r['id']}: no goal while goals exist")
        for p in r.get("produced_by", []):
            if p not in prids:
                unk(f"result {r['id']}: produced_by '{p}' unknown")
        for f in r.get("feeds", []):
            if xok(f) is False:
                errors.append(f"result {r['id']}: feeds cross-stack '{f}' undeclared")
    for g in gids - {r.get("goal") for r in doc.get("results", [])}:
        warnings.append(f"goal {g}: no results (a goal with no path to it)")

    # result-first: a process that produces nothing and that no result names is "coding for
    # coding's sake". Either direction of the link counts - `produces` here, or `produced_by` there.
    claimed = {p for r in doc.get("results", []) for p in r.get("produced_by", [])}
    for p in doc.get("processes", []):
        if not p.get("produces") and p["id"] not in claimed:
            unk(f"process {p['id']}: no result - `produces` is empty and no result lists it in "
                f"`produced_by` (a process with no result is coding for coding's sake)")
        for r in p.get("produces", []):
            if r not in rids:
                unk(f"process {p['id']}: produces '{r}' unknown")
        for t in p.get("tasks", []):
            if t.get("workflow") and t["workflow"] not in wids:
                unk(f"process {p['id']} task {t['id']}: workflow unknown")
            h = t.get("human", {})
            if h.get("role") and roleids and h["role"] not in roleids:
                unk(f"process {p['id']} task {t['id']}: human role unknown")

    for w in doc.get("workflows", []):
        if w.get("software") and w["software"] not in sids:
            unk(f"workflow {w['id']}: software unknown")
        if "trigger" in w:
            errors.append(f"workflow {w['id']}: legacy inline trigger (use the triggers collection)")
        for t in w.get("triggers", []):
            if t not in trids:
                unk(f"workflow {w['id']}: trigger '{t}' unknown")
            used_triggers.add(t)
        for u in w.get("uses", []):
            if xok(u) is None and u not in sids | eids:
                unk(f"workflow {w['id']}: uses '{u}' unknown")
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
                unk(f"entity {e['id']}: store software '{sw}' unknown")
            if s.get("instance") and ":" not in sw and s["instance"] not in inst.get(sw, set()):
                unk(f"entity {e['id']}: instance '{s['instance']}' not on '{sw}'")
        for rel in e.get("relations", []):
            if rel not in eids:
                unk(f"entity {e['id']}: relation '{rel}' unknown")

    for i in doc.get("interfaces", []):
        if i.get("software") and i["software"] not in sids:
            unk(f"interface {i['id']}: software unknown")
        for ii in i.get("instances", []):
            if ii not in inst.get(i.get("software", ""), set()):
                unk(f"interface {i['id']}: instance '{ii}' not on its software")
        for rel in i.get("related", []):
            if rel not in iids:
                unk(f"interface {i['id']}: related '{rel}' unknown")
        for x in i.get("entities", []):
            if x not in eids:
                unk(f"interface {i['id']}: entity '{x}' unknown")
        for x in i.get("workflows", []):
            if x not in wids:
                unk(f"interface {i['id']}: workflow '{x}' unknown")
        for x in i.get("roles", []):
            if roleids and x not in roleids:
                unk(f"interface {i['id']}: role '{x}' unknown")

    for m in doc.get("connections", {}).get("mcp", []):
        if m.get("software") and m["software"] not in sids:
            unk(f"mcp {m['id']}: software unknown")
        if m.get("instance") and m["instance"] not in inst.get(m.get("software", ""), set()):
            unk(f"mcp {m['id']}: instance unknown")

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
                unk(f"{who} {a['id']}: invocation interface unknown")
            if via == "workflow" and inv.get("workflow") not in wids:
                unk(f"{who} {a['id']}: invocation workflow unknown")
            if via == "trigger":
                if inv.get("trigger") not in trids:
                    unk(f"{who} {a['id']}: invocation trigger unknown")
                else:
                    used_triggers.add(inv["trigger"])

    for a in sa:
        for acc in a.get("access", []):
            if xok(acc) is None and acc not in mids | sids | iids:
                unk(f"stack_agent {a['id']}: access '{acc}' unknown")
        for cp in a.get("context_packs", []):
            if packs and cp not in packs:
                unk(f"stack_agent {a['id']}: context pack '{cp}' unknown")
        for d in a.get("delegates_to", []):
            if d not in said:
                unk(f"stack_agent {a['id']}: delegates_to '{d}' unknown")
            elif hier.get(a["id"]) and hier.get(d) and HIER[hier[a["id"]]] <= HIER[hier[d]]:
                errors.append(f"stack_agent {a['id']}: delegation must go downward")
        check_invocations(a, "stack_agent")

    # an API a managed agent may call is a declared software id, or an API declared in connections.apis
    api_ids = sids | {x.get("software") for x in doc.get("connections", {}).get("apis", [])}
    for a in ag2.get("managed_agents", []):
        t = a.get("tools", {})
        for m in t.get("mcp", []):
            if m not in mids:
                unk(f"managed_agent {a['id']}: tools.mcp '{m}' unknown")
        for x in t.get("apis", []):
            if xok(x) is False:
                errors.append(f"managed_agent {a['id']}: tools.apis cross-stack '{x}' undeclared")
            elif xok(x) is None and x not in api_ids:
                unk(f"managed_agent {a['id']}: tools.apis '{x}' unknown")
        for w in t.get("workflows", []):
            if w not in wids:
                unk(f"managed_agent {a['id']}: tools.workflows '{w}' unknown")
        for ro in a.get("available_to", []):
            if roleids and ro not in roleids:
                unk(f"managed_agent {a['id']}: available_to '{ro}' unknown")
        check_invocations(a, "managed_agent")

    for t in trids - used_triggers:
        hint = f" (may be used by a workflow of prototype {open_ref}; pass --prototype)" if open_ref else ""
        warnings.append(f"trigger {t}: referenced by no workflow or agent{hint}")

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
                unk(f"plugin {pid}: scope '{sc}' resolves to no declared element")
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
    ap = argparse.ArgumentParser(description="Validate macstack.json files: schema pass + integrity pass.")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--schema", default=str(pathlib.Path(__file__).parent.parent / "schema/macstack.schema.json"))
    ap.add_argument("--categories", default=None, help="path or URL to software-categories.json (registry)")
    ap.add_argument("--coverage-areas", default=None, help="path or URL to coverage-areas.json (registry)")
    ap.add_argument("--marketplace", action="append", default=[], metavar="PATH_OR_URL",
                    help="plugin marketplace.json (repeatable): plugin ids and skill prefixes must be in one of them")
    ap.add_argument("--prototype", action="append", default=[], metavar="PATH_OR_URL",
                    help="the file `prototype` names, merged by id before the integrity pass; repeatable, "
                         "nearest parent first. One linted file only")
    ap.add_argument("--root", default=None, metavar="DIR",
                    help="project root: workflows[].source must exist relative to it")
    args = ap.parse_args()

    if args.prototype and len(args.files) != 1:
        ap.error("--prototype applies to one file; lint files that have different prototypes separately")
    if args.root is not None and not os.path.isdir(args.root):
        ap.error(f"--root {args.root}: not a directory")

    schema = load_or_exit("schema", args.schema)
    categories = None
    if args.categories:
        categories = {c["id"] for c in load_or_exit("categories", args.categories)["categories"]}
    coverage_areas = None
    if args.coverage_areas:
        coverage_areas = {a["id"] for a in load_or_exit("coverage-areas", args.coverage_areas)["areas"]}
    marketplace = load_marketplaces(args.marketplace) if args.marketplace else None
    prototypes = [(ref, load_or_exit("prototype", ref)) for ref in args.prototype]

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
        errors, warnings = lint(doc, categories, coverage_areas, prototypes=prototypes or None,
                                doc_ref=str(pathlib.Path(f).resolve()), root=args.root, marketplace=marketplace)
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
