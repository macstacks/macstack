#!/usr/bin/env python3
"""Tests for scripts/lint.py and for the schema/examples invariants the linter relies on.

Plain unittest, no network, nothing written outside a temp dir:

    python3 scripts/test_lint.py            # all tests
    python3 scripts/test_lint.py -v         # one line per test
    python3 scripts/test_lint.py LintSecrets  # one class

Every fixture is built from a shipped example (acme-website), mutated in memory, so a
schema change that breaks the examples breaks these tests first. Tests that need the
`jsonschema` package are skipped without it, exactly as lint.py skips its schema pass.
"""
import copy
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
import lint  # noqa: E402

LINT = HERE / "lint.py"
SCHEMA_PATH = REPO / "schema" / "macstack.schema.json"
EXAMPLES = sorted((REPO / "examples").glob("*.macstack.json"))

try:
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None


def example(name="acme-website", keep_prototype=False):
    """A shipped example as a mutable fixture. Its `prototype` is dropped unless asked for: with a
    prototype set, unknown ids are warnings by design, and most tests want them to be errors."""
    with open(REPO / "examples" / f"{name}.macstack.json") as fh:
        doc = json.load(fh)
    if not keep_prototype:
        doc.pop("prototype", None)
    return doc


def schema():
    with open(SCHEMA_PATH) as fh:
        return json.load(fh)


def run(doc, categories=None, coverage_areas=None, **kw):
    """lint.lint() on a deep copy, so a test can never leak a mutation into another."""
    return lint.lint(copy.deepcopy(doc), categories, coverage_areas, **kw)


def has(messages, *needles):
    """True when one message contains every needle (case-insensitive)."""
    return any(all(n.lower() in m.lower() for n in needles) for m in messages)


def cli(*args):
    p = subprocess.run([sys.executable, str(LINT), *map(str, args)], capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


class Tmp(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._t.name)
        self.addCleanup(self._t.cleanup)

    def write(self, name, data):
        p = self.tmp / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data if isinstance(data, str) else json.dumps(data, indent=2))
        return p


# --------------------------------------------------------------------------- baseline

class Baseline(unittest.TestCase):
    def test_examples_have_no_errors_and_no_warnings(self):
        for f in EXAMPLES:
            with open(f) as fh:
                errors, warnings = run(json.load(fh))
            self.assertEqual(errors, [], f.name)
            self.assertEqual(warnings, [], f.name)

    @unittest.skipUnless(jsonschema, "jsonschema not installed")
    def test_examples_validate_against_draft_2020_12(self):
        s = schema()
        jsonschema.Draft202012Validator.check_schema(s)
        for f in EXAMPLES:
            with open(f) as fh:
                jsonschema.Draft202012Validator(s).validate(json.load(fh))


# --------------------------------------------------------------------------- S6a

class ProcessWithoutResult(unittest.TestCase):
    def test_process_with_no_result_in_either_direction_is_an_error(self):
        doc = example()
        doc["processes"].append({"id": "coding-for-coding", "name": "No point", "tasks": []})
        errors, _ = run(doc)
        self.assertTrue(has(errors, "process coding-for-coding", "no result"), errors)

    def test_empty_produces_list_counts_as_no_result(self):
        doc = example()
        doc["processes"].append({"id": "empty", "name": "Empty", "produces": []})
        errors, _ = run(doc)
        self.assertTrue(has(errors, "process empty", "no result"), errors)

    def test_listed_by_a_result_is_enough_even_with_empty_produces(self):
        doc = example()
        doc["processes"].append({"id": "claimed", "name": "Claimed", "produces": []})
        doc["results"][0].setdefault("produced_by", []).append("claimed")
        errors, _ = run(doc)
        self.assertFalse(has(errors, "process claimed"), errors)

    def test_produces_alone_is_enough_even_when_no_result_lists_it(self):
        # three of the four shipped examples rely on this direction only
        doc = example()
        doc["processes"].append({"id": "one-way", "name": "One way", "produces": [doc["results"][0]["id"]]})
        errors, _ = run(doc)
        self.assertFalse(has(errors, "process one-way"), errors)


# --------------------------------------------------------------------------- S6b

class DuplicateIds(unittest.TestCase):
    def test_duplicate_software_id(self):
        doc = example()
        doc["software"].append(copy.deepcopy(doc["software"][0]))
        errors, _ = run(doc)
        self.assertTrue(has(errors, "duplicate", "software", doc["software"][0]["id"]), errors)

    def test_every_id_collection_that_collapses_into_a_set_is_checked(self):
        doc = example()
        cases = {
            "goals": doc["goals"], "results": doc["results"], "processes": doc["processes"],
            "triggers": doc["triggers"], "workflows": doc["workflows"], "entities": doc["entities"],
            "interfaces": doc["interfaces"], "roles": doc["roles"],
        }
        for section, items in cases.items():
            d = copy.deepcopy(doc)
            d[section].append(copy.deepcopy(d[section][0]))
            errors, _ = run(d)
            self.assertTrue(has(errors, "duplicate", section, d[section][0]["id"]), (section, errors))

    def test_duplicate_mcp_connection_pack_and_agents(self):
        doc = example()
        doc["connections"]["mcp"].append(copy.deepcopy(doc["connections"]["mcp"][0]))
        doc["context"]["packs"].append(copy.deepcopy(doc["context"]["packs"][0]))
        doc["agents"]["managed_agents"].append(copy.deepcopy(doc["agents"]["managed_agents"][0]))
        errors, _ = run(doc)
        self.assertTrue(has(errors, "duplicate", "connections.mcp", doc["connections"]["mcp"][0]["id"]), errors)
        self.assertTrue(has(errors, "duplicate", "context.packs", doc["context"]["packs"][0]["name"]), errors)
        self.assertTrue(has(errors, "duplicate", "managed_agents", doc["agents"]["managed_agents"][0]["id"]), errors)

    def test_duplicate_instance_inside_one_software(self):
        doc = example()
        sw = doc["software"][0]
        sw["instances"].append(copy.deepcopy(sw["instances"][0]))
        errors, _ = run(doc)
        self.assertTrue(has(errors, "duplicate", "instance", sw["instances"][0]["id"], sw["id"]), errors)

    def test_same_instance_id_on_two_different_software_is_fine(self):
        doc = example()
        a, b = doc["software"][0], doc["software"][1]
        b["instances"] = [dict(b["instances"][0], id=a["instances"][0]["id"])]
        errors, _ = run(doc)
        self.assertFalse(has(errors, "duplicate"), errors)

    def test_duplicate_task_id_inside_one_process(self):
        doc = example()
        p = doc["processes"][0]
        p["tasks"].append(copy.deepcopy(p["tasks"][0]))
        errors, _ = run(doc)
        self.assertTrue(has(errors, "duplicate", "task", p["tasks"][0]["id"], p["id"]), errors)


# --------------------------------------------------------------------------- S6c

def proto_doc(**extra):
    d = {"macstack": "1.0", "name": "proto", "version": "1.0.0", "description": "a prototype",
         "software": [{"id": "inherited-sw", "category": "cms", "type": "ready_made",
                       "instances": [{"id": "inherited-prod"}]}]}
    d.update(extra)
    return d


def child_using_inherited():
    doc = example()
    doc["prototype"] = "github:acme-templates/proto"
    doc["workflows"][0]["software"] = "inherited-sw"
    return doc


class Prototype(Tmp):
    def test_without_the_prototype_file_unknown_ids_are_warnings_that_name_the_prototype(self):
        errors, warnings = run(child_using_inherited())
        self.assertFalse(has(errors, "unknown"), errors)
        self.assertTrue(has(warnings, "software unknown", "may come from prototype github:acme-templates/proto",
                            "--prototype"), warnings)

    def test_without_a_prototype_field_unknown_ids_stay_errors(self):
        doc = child_using_inherited()
        del doc["prototype"]
        errors, warnings = run(doc)
        self.assertTrue(has(errors, "software unknown"), errors)
        self.assertFalse(has(warnings, "may come from prototype"), warnings)

    def test_merged_prototype_resolves_inherited_ids(self):
        errors, warnings = run(child_using_inherited(), prototypes=[("proto.json", proto_doc())])
        self.assertFalse(has(errors, "unknown"), errors)
        self.assertFalse(has(warnings, "may come from prototype"), warnings)

    def test_merged_prototype_that_lacks_the_id_makes_it_an_error_again(self):
        other = proto_doc(software=[{"id": "something-else", "category": "cms", "type": "ready_made"}])
        errors, _ = run(child_using_inherited(), prototypes=[("proto.json", other)])
        self.assertTrue(has(errors, "software unknown"), errors)

    def test_prototype_exclude_removes_an_inherited_element(self):
        doc = child_using_inherited()
        doc["prototype_exclude"] = ["software.inherited-sw"]
        errors, _ = run(doc, prototypes=[("proto.json", proto_doc())])
        self.assertTrue(has(errors, "software unknown"), errors)

    def test_child_overrides_an_inherited_element_by_id(self):
        doc = child_using_inherited()
        doc["software"].append({"id": "inherited-sw", "category": "cms", "type": "ready_made",
                                "instances": [{"id": "child-prod"}]})
        doc["triggers"][0].update(software="inherited-sw", instance="child-prod")
        errors, _ = run(doc, prototypes=[("proto.json", proto_doc())])
        self.assertFalse(has(errors, "unknown"), errors)
        self.assertFalse(has(errors, "duplicate"), errors)  # merge by id is not a duplicate

    def test_chain_is_merged_nearest_first(self):
        doc = child_using_inherited()
        mid = proto_doc(software=[], prototype="/protos/root.json")
        root = proto_doc()
        errors, warnings = run(doc, prototypes=[("/protos/mid.json", mid), ("/protos/root.json", root)])
        self.assertFalse(has(errors, "unknown"), errors)
        self.assertFalse(has(warnings, "may come from prototype"), warnings)

    def test_chain_that_continues_past_the_last_file_downgrades_unknown_ids_to_warnings(self):
        doc = child_using_inherited()
        mid = proto_doc(software=[], prototype="github:acme-templates/grand-proto")
        errors, warnings = run(doc, prototypes=[("/protos/mid.json", mid)])
        self.assertFalse(has(errors, "unknown"), errors)
        self.assertTrue(has(warnings, "may come from prototype github:acme-templates/grand-proto"), warnings)

    def test_cycle_back_to_the_linted_file_is_an_error(self):
        doc = child_using_inherited()
        doc["prototype"] = "/protos/p1.json"
        p1 = proto_doc(prototype="/work/child.macstack.json")
        errors, _ = run(doc, prototypes=[("/protos/p1.json", p1)], doc_ref="/work/child.macstack.json")
        self.assertTrue(has(errors, "prototype cycle"), errors)

    def test_cycle_between_two_prototypes_is_an_error(self):
        doc = child_using_inherited()
        p1 = proto_doc(prototype="/protos/p2.json")
        p2 = proto_doc(prototype="/protos/p1.json")
        errors, _ = run(doc, prototypes=[("/protos/p1.json", p1), ("/protos/p2.json", p2)], doc_ref="/work/c.json")
        self.assertTrue(has(errors, "prototype cycle"), errors)

    def test_cycle_via_a_folder_ref_resolves_to_its_macstack_json(self):
        doc = child_using_inherited()
        p1 = proto_doc(prototype="/work/proj")  # a folder -> /work/proj/macstack.json is the linted file
        errors, _ = run(doc, prototypes=[("/protos/p1.json", p1)], doc_ref="/work/proj/macstack.json")
        self.assertTrue(has(errors, "prototype cycle"), errors)

    def test_the_same_prototype_listed_twice_is_a_cycle(self):
        doc = child_using_inherited()
        errors, _ = run(doc, prototypes=[("/protos/p1.json", proto_doc()), ("/protos/p1.json", proto_doc())])
        self.assertTrue(has(errors, "prototype cycle"), errors)

    def test_cli_prototype_flag_merges_the_file(self):
        child = self.write("child.macstack.json", child_using_inherited())
        proto = self.write("proto.macstack.json", proto_doc())
        code, out = cli(child, "--prototype", proto)
        self.assertEqual(code, 0, out)
        self.assertNotIn("may come from prototype", out)

    def test_cli_without_the_flag_prints_the_warning_and_still_passes(self):
        child = self.write("child.macstack.json", child_using_inherited())
        code, out = cli(child)
        self.assertEqual(code, 0, out)
        self.assertIn("pass --prototype", out)

    def test_cli_detects_a_cycle_through_files(self):
        child_doc = child_using_inherited()
        child = self.write("child.macstack.json", child_doc)
        proto = self.write("proto.macstack.json", proto_doc(prototype=str(child)))
        child_doc["prototype"] = str(proto)
        child.write_text(json.dumps(child_doc))
        code, out = cli(child, "--prototype", proto)
        self.assertEqual(code, 1, out)
        self.assertIn("prototype cycle", out)

    def test_cli_prototype_flag_with_several_files_is_refused(self):
        a = self.write("a.macstack.json", example())
        b = self.write("b.macstack.json", example())
        p = self.write("p.macstack.json", proto_doc())
        code, out = cli(a, b, "--prototype", p)
        self.assertEqual(code, 2, out)
        self.assertIn("--prototype", out)


# --------------------------------------------------------------------------- S6d

SECRET_SAMPLES = {
    "stripe live": "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
    "stripe test": "sk_test_" + "4eC39HqLyjWDarjtT1zdp7dc",
    "slack bot": "xoxb-" + "1234567890-abcdefghijkl",
    "github pat": "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789",
    "github fine-grained": "github_pat_" + "11ABCDEFG0abcdefghijkl_mnop",
    "aws access key": "AKIA" + "IOSFODNN7EXAMPLE",
    "private key": "-----BEGIN " + "RSA PRIVATE KEY-----",
}


class Secrets(Tmp):
    def test_each_secret_pattern_in_accesses_env_is_an_error_naming_the_path_not_the_value(self):
        for label, value in SECRET_SAMPLES.items():
            doc = example()
            doc["resources"]["accesses"][0]["env"] = value
            errors, warnings = run(doc)
            self.assertTrue(has(errors, "resources.accesses[0].env", "secret"), (label, errors))
            for m in errors + warnings:
                self.assertNotIn(value, m, label)
                self.assertNotIn(value[:12], m, label)

    def test_other_env_like_fields_are_checked_too(self):
        value = SECRET_SAMPLES["stripe live"]
        doc = example()
        doc["connections"]["mcp"][0]["url_env"] = value
        doc["connections"]["apis"][0]["url_env"] = value
        errors, _ = run(doc)
        self.assertTrue(has(errors, "connections.mcp[0].url_env", "secret"), errors)
        self.assertTrue(has(errors, "connections.apis[0].url_env", "secret"), errors)

    def test_real_env_key_names_are_fine(self):
        doc = example()
        for name in ("STRIPE_SECRET_KEY", "GITHUB_PAT", "AWS_ACCESS_KEY_ID", "SLACK_BOT_TOKEN"):
            doc["resources"]["accesses"][0]["env"] = name
            errors, _ = run(doc)
            self.assertFalse(has(errors, "secret"), (name, errors))

    def test_cli_never_prints_the_value(self):
        value = SECRET_SAMPLES["github pat"]
        doc = example()
        doc["resources"]["accesses"][0]["env"] = value
        f = self.write("leak.macstack.json", doc)
        code, out = cli(f)
        self.assertEqual(code, 1, out)
        self.assertIn("resources.accesses[0].env", out)
        self.assertNotIn(value, out)
        self.assertNotIn("ghp_abc", out)


# --------------------------------------------------------------------------- S6e

class WorkflowSource(Tmp):
    def doc_with_source(self, source):
        doc = example()
        doc["workflows"][0]["source"] = source
        return doc

    def test_missing_source_file_is_an_error_when_root_is_given(self):
        root = self.tmp / "proj"
        root.mkdir()
        errors, _ = run(self.doc_with_source("src/workflows/lead-intake.ts"), root=root)
        self.assertTrue(has(errors, "workflow wf-lead-intake", "source", "src/workflows/lead-intake.ts", "not found"), errors)

    def test_existing_source_file_passes(self):
        self.write("proj/src/workflows/lead-intake.ts", "export {}")
        errors, _ = run(self.doc_with_source("src/workflows/lead-intake.ts"), root=self.tmp / "proj")
        self.assertFalse(has(errors, "source"), errors)

    def test_a_directory_is_a_valid_source(self):
        (self.tmp / "proj" / "src" / "trigger").mkdir(parents=True)
        errors, _ = run(self.doc_with_source("src/trigger"), root=self.tmp / "proj")
        self.assertFalse(has(errors, "source"), errors)

    def test_without_root_nothing_is_checked(self):
        errors, warnings = run(self.doc_with_source("src/does/not/exist.ts"))
        self.assertFalse(has(errors, "source"), errors)

    def test_absolute_and_escaping_paths_are_errors(self):
        root = self.tmp / "proj"
        root.mkdir()
        for bad in ("/etc/passwd", "../outside.ts", "src/../../outside.ts"):
            errors, _ = run(self.doc_with_source(bad), root=root)
            self.assertTrue(has(errors, "workflow wf-lead-intake", "source", "relative to the project root"), (bad, errors))

    def test_cli_root_flag(self):
        self.write("proj/src/a.ts", "x")
        good = self.write("proj/good.macstack.json", self.doc_with_source("src/a.ts"))
        bad = self.write("proj/bad.macstack.json", self.doc_with_source("src/missing.ts"))
        self.assertEqual(cli(good, "--root", self.tmp / "proj")[0], 0)
        code, out = cli(bad, "--root", self.tmp / "proj")
        self.assertEqual(code, 1, out)
        self.assertIn("src/missing.ts", out)
        self.assertEqual(cli(bad)[0], 0)  # no --root, no check


# --------------------------------------------------------------------------- S6f

class ManagedAgentTools(unittest.TestCase):
    def agent(self, **tools):
        doc = example()
        doc["agents"]["managed_agents"][0]["tools"] = tools
        return doc

    def test_unknown_api_is_an_error(self):
        errors, _ = run(self.agent(apis=["no-such-api"]))
        self.assertTrue(has(errors, "managed_agent content-writer", "tools.apis", "no-such-api"), errors)

    def test_api_may_be_a_software_id_or_a_declared_connections_api(self):
        errors, _ = run(self.agent(apis=["directus", "mailgun"]))
        self.assertFalse(has(errors, "tools.apis"), errors)

    def test_api_declared_only_in_connections_apis_resolves(self):
        doc = self.agent(apis=["external-geocoder"])
        doc["connections"]["apis"].append({"software": "external-geocoder", "kind": "rest"})
        errors, _ = run(doc)
        self.assertFalse(has(errors, "tools.apis"), errors)

    def test_existing_mcp_and_workflow_checks_still_fire(self):
        errors, _ = run(self.agent(mcp=["nope-mcp"], workflows=["wf-nope"]))
        self.assertTrue(has(errors, "tools.mcp", "nope-mcp"), errors)
        self.assertTrue(has(errors, "tools.workflows", "wf-nope"), errors)

    def test_native_tools_are_free_text(self):
        errors, _ = run(self.agent(native=["knowledge-base-retrieve", "anything-goes"]))
        self.assertFalse(has(errors, "native"), errors)


# --------------------------------------------------------------------------- S6g

def marketplace(names, renames=None):
    return {"names": set(names), "renames": renames or {}}


PUBLIC = marketplace(["directus-dev", "nextjs-dev", "nextjs-provision", "trigger-dev", "seo-dev",
                      "stack-directus-nextjs-trigger", "plane-ops", "web-search-dev"],
                     {"media-hosting-ops": None, "firecrawl": "web-search-dev"})


class PluginNames(Tmp):
    def test_unknown_plugin_id_is_an_error(self):
        doc = example()
        doc["context"]["plugins"]["technology"].append({"id": "ghost-dev", "covers": ["software"], "scope": ["directus"]})
        errors, _ = run(doc, marketplace=PUBLIC)
        self.assertTrue(has(errors, "plugin ghost-dev", "marketplace"), errors)

    def test_removed_and_renamed_plugins_say_so(self):
        doc = example()
        doc["context"]["plugins"]["process"] = [{"id": "media-hosting-ops", "covers": ["processes"]}]
        doc["context"]["plugins"]["technology"].append({"id": "firecrawl", "covers": ["integrations"]})
        errors, _ = run(doc, marketplace=PUBLIC)
        self.assertTrue(has(errors, "media-hosting-ops", "removed"), errors)
        self.assertTrue(has(errors, "firecrawl", "renamed", "web-search-dev"), errors)

    def test_bare_slug_form_and_pack_members_are_checked(self):
        doc = example()
        doc["context"]["plugins"]["technology"].append("bare-ghost")
        doc["context"]["packs"][0]["plugins"].append("pack-ghost")
        errors, _ = run(doc, marketplace=PUBLIC)
        self.assertTrue(has(errors, "plugin bare-ghost"), errors)
        self.assertTrue(has(errors, "pack-ghost", "marketplace"), errors)

    def test_skill_reference_prefix_must_exist(self):
        doc = example()
        doc["processes"][0]["skill"] = "ghost-dev:do-it"
        errors, _ = run(doc, marketplace=PUBLIC)
        self.assertTrue(has(errors, "process lead-capture", "skill", "ghost-dev:do-it", "marketplace"), errors)

    def test_known_names_pass_and_no_flag_means_no_check(self):
        errors, _ = run(example(), marketplace=PUBLIC)
        self.assertEqual(errors, [])
        doc = example()
        doc["context"]["plugins"]["technology"].append({"id": "ghost-dev", "covers": ["software"]})
        self.assertFalse(has(run(doc)[0], "marketplace"))

    def test_cli_marketplace_flag_is_repeatable_and_unions_the_names(self):
        doc = example()
        doc["context"]["plugins"]["technology"].append({"id": "second-market-dev", "covers": ["software"], "scope": ["directus"]})
        f = self.write("a.macstack.json", doc)
        m1 = self.write("m1.json", {"name": "one", "plugins": [{"name": n} for n in PUBLIC["names"]]})
        m2 = self.write("m2.json", {"name": "two", "plugins": [{"name": "second-market-dev"}]})
        code, out = cli(f, "--marketplace", m1)
        self.assertEqual(code, 1, out)
        self.assertIn("second-market-dev", out)
        code, out = cli(f, "--marketplace", m1, "--marketplace", m2)
        self.assertEqual(code, 0, out)

    def test_cli_reads_the_renames_map(self):
        doc = example()
        doc["context"]["plugins"]["technology"].append({"id": "old-name", "covers": ["software"], "scope": ["directus"]})
        f = self.write("a.macstack.json", doc)
        m = self.write("m.json", {"name": "m", "plugins": [{"name": n} for n in PUBLIC["names"]],
                                  "renames": {"old-name": "directus-dev"}})
        code, out = cli(f, "--marketplace", m)
        self.assertEqual(code, 1, out)
        self.assertIn("renamed to directus-dev", out)


# --------------------------------------------------------------------------- S6h

class DeprecatedStatuses(unittest.TestCase):
    def doc_with(self, section, item):
        doc = example()
        doc.setdefault("lifecycle", {})[section] = [item]
        return doc

    def test_each_deprecated_token_warns_with_its_replacement(self):
        cases = [("doing", "in_progress"), ("dropped", "cancelled"), ("blocked", "blocked_by")]
        for token, replacement in cases:
            _, warnings = run(self.doc_with("next_steps", {"id": "M1-T1", "status": token}))
            self.assertTrue(has(warnings, "M1-T1", f"'{token}'", "deprecated", replacement), (token, warnings))
            _, warnings = run(self.doc_with("milestones", {"id": "M1", "status": token}))
            self.assertTrue(has(warnings, "M1", f"'{token}'", "deprecated", replacement), (token, warnings))

    def test_tracker_five_do_not_warn(self):
        for token in ("backlog", "todo", "in_progress", "done", "cancelled"):
            _, warnings = run(self.doc_with("next_steps", {"id": "M1-T1", "status": token}))
            self.assertFalse(has(warnings, "deprecated"), (token, warnings))
            _, warnings = run(self.doc_with("milestones", {"id": "M1", "status": token}))
            self.assertFalse(has(warnings, "deprecated"), (token, warnings))

    def test_deprecated_tokens_are_warnings_never_errors(self):
        errors, _ = run(self.doc_with("next_steps", {"id": "M1-T1", "status": "doing"}))
        self.assertEqual(errors, [])

    def test_legacy_free_text_next_steps_are_ignored(self):
        _, warnings = run(self.doc_with("next_steps", "Launch wf-seo-audit"))
        self.assertFalse(has(warnings, "deprecated"), warnings)

    @unittest.skipUnless(jsonschema, "jsonschema not installed")
    def test_deprecated_tokens_still_validate_against_the_schema(self):
        doc = self.doc_with("next_steps", {"id": "M1-T1", "status": "blocked"})
        doc["lifecycle"]["milestones"] = [{"id": "M1", "status": "dropped"}]
        jsonschema.Draft202012Validator(schema()).validate(doc)


# --------------------------------------------------------------------------- schema contract (rev 18)

class SchemaRev18(unittest.TestCase):
    def setUp(self):
        self.s = schema()
        self.defs = self.s["$defs"]
        self.files = self.s["properties"]["docs"]["properties"]["files"]["properties"]

    def test_comment_holds_only_the_latest_revision_note(self):
        c = self.s["$comment"]
        self.assertTrue(c.startswith("rev 18 - 2026-10-04:"), c[:60])
        self.assertEqual(len(re.findall(r"\brev \d+ - \d{4}-", c)), 1, "older revision notes must not be kept")

    def test_docs_files_carry_the_v3_document_keys(self):
        for key in ("ledger", "requirements", "review", "inbox_manifest"):
            self.assertIn(key, self.files, key)
            self.assertTrue(self.files[key].get("description"), key)

    def test_ledger_points_at_history_ledger_jsonl_and_log_is_a_deprecated_alias(self):
        self.assertIn("history/ledger.jsonl", self.files["ledger"]["description"])
        log = self.files["log"]["description"].lower()
        self.assertIn("deprecated", log)
        self.assertIn("ledger", log)

    def test_stale_v2_wording_is_gone(self):
        body = {k: v for k, v in self.s.items() if k != "$comment"}  # the revision note may quote the old wording
        text = json.dumps(body)
        self.assertNotIn("journal table", text)
        self.assertNotIn("one test per bullet", text)
        self.assertIn("case", self.files["test_cases"]["description"].lower())

    def test_changelog_is_curated_from_the_ledger_not_the_log(self):
        d = self.files["changelog"]["description"]
        self.assertIn("ledger", d)
        self.assertNotIn("of the log", d)

    def test_docs_dirs_is_declared_informational(self):
        d = self.s["properties"]["docs"]["properties"]["dirs"]["description"].lower()
        self.assertTrue("optional" in d or "informational" in d, d)

    @unittest.skipUnless(jsonschema, "jsonschema not installed")
    def test_a_v3_docs_section_validates(self):
        doc = example()
        doc["docs"] = {"files": {k: {"path": f"{k}.md"} for k in
                                 ("ledger", "requirements", "review", "inbox_manifest", "log", "tasks")}}
        jsonschema.Draft202012Validator(self.s).validate(doc)

    def test_coverage_area_description_names_seo_and_points_at_the_registry(self):
        d = self.defs["coverageArea"]["description"]
        self.assertIn("seo", re.findall(r"[a-z][a-z-]*", d))
        self.assertIn("coverage-areas.json", d)

    def test_deprecated_statuses_are_marked_in_both_refs_and_still_accepted(self):
        for ref in ("taskRef", "milestoneRef"):
            st = self.defs[ref]["properties"]["status"]
            for token in ("doing", "blocked", "dropped"):
                self.assertIn(token, st["enum"], (ref, token))
            d = st["description"]
            self.assertIn("deprecated", d.lower(), ref)
            for replacement in ("in_progress", "cancelled", "dependency"):
                self.assertIn(replacement, d, (ref, replacement))
        self.assertIn("milestone", self.defs["milestoneRef"]["properties"]["status"]["description"].lower())

    def test_human_gate_has_no_none_and_the_descriptions_say_a_workflow_only_task_has_no_human_block(self):
        human = self.s["properties"]["processes"]["items"]["properties"]["tasks"]["items"]["properties"]["human"]
        self.assertNotIn("none", human["properties"]["gate"]["enum"])
        self.assertIn("no `human` block", human["description"])
        gate = human["properties"]["gate"]["description"]
        self.assertIn("no `human` block", gate)
        self.assertIn("none", gate)


# --------------------------------------------------------------------------- examples are public-only

class ExamplesArePublic(unittest.TestCase):
    def test_every_example_lists_only_the_public_marketplace(self):
        for f in EXAMPLES:
            with open(f) as fh:
                doc = json.load(fh)
            self.assertEqual(doc["context"]["marketplaces"], ["agents-store-claude-plugins"], f.name)

    def test_no_example_uses_a_removed_or_private_plugin(self):
        gone = {"media-hosting-ops", "tg-client"}
        for f in EXAMPLES:
            text = f.read_text()
            for name in gone:
                self.assertNotIn(f'"{name}"', text, f"{f.name}: {name}")


if __name__ == "__main__":
    unittest.main()
