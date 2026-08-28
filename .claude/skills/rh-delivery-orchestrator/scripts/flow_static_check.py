#!/usr/bin/env python3
"""
flow_static_check.py — deterministic static analysis for Salesforce Flow metadata.

Runs with no network, no org connection, and no browser. Parses one or more
.flow-meta.xml files and reports rule violations as JSON plus a human-readable
summary.

Usage:
    python3 flow_static_check.py force-app/main/default/flows/My_Flow.flow-meta.xml
    python3 flow_static_check.py force-app/main/default/flows/ --api-version 67.0
    python3 flow_static_check.py <paths...> --story S-001042 --json-out qa/static.json

Exit codes:
    0  no FAIL findings (WARN findings may still be present)
    1  at least one FAIL finding
    2  could not parse a file / bad invocation

Findings are one of:
    FAIL  blocks QA — the flow violates a radianHub standard
    WARN  needs a human decision — likely a problem, not provably one
    INFO  contextual, never blocks
"""

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

NS = {"sf": "http://soap.sforce.com/2006/04/metadata"}
NS_PREFIX = "{http://soap.sforce.com/2006/04/metadata}"

# Element collections that represent executable nodes on the canvas.
NODE_TAGS = {
    "actionCalls", "apexPluginCalls", "assignments", "collectionProcessors",
    "customErrors", "decisions", "loops", "orchestratedStages", "recordCreates",
    "recordDeletes", "recordLookups", "recordRollbacks", "recordUpdates",
    "screens", "steps", "subflows", "transforms", "waits",
}

# Nodes that hit the database or an external system.
DML_TAGS = {"recordCreates", "recordDeletes", "recordUpdates"}
QUERY_TAGS = {"recordLookups"}
IO_TAGS = DML_TAGS | QUERY_TAGS | {"actionCalls", "apexPluginCalls", "subflows"}

# Tags that carry a <targetReference> to another node.
CONNECTOR_TAGS = {
    "connector": "next",
    "nextValueConnector": "loopBody",
    "noMoreValuesConnector": "loopExit",
    "defaultConnector": "default",
    "defaultConnectorLabel": None,
    "faultConnector": "fault",
    "nextOrFinishConnector": "next",
    "backConnector": "back",
    "noMatchConnector": "next",
}

# Salesforce Ids are exactly 15 or 18 alphanumeric chars, mix letters and
# digits, and in practice contain a run of zeros from the instance segment.
# Anchoring on length and confirming those traits keeps false positives on
# ordinary words and API names low. Reported as WARN, never FAIL.
ID_CANDIDATE_RE = re.compile(r"\b[a-zA-Z0-9]{15}(?:[a-zA-Z0-9]{3})?\b")


def looks_like_id(token):
    return (
        len(token) in (15, 18)
        and re.search(r"0{3,}", token) is not None
        and any(c.isdigit() for c in token)
        and any(c.isalpha() for c in token)
    )

SCREEN_PROCESS_TYPES = {"Flow"}
AUTOLAUNCHED_PROCESS_TYPES = {"AutoLaunchedFlow"}


def strip_ns(tag):
    return tag.replace(NS_PREFIX, "")


def text_of(parent, child_name):
    el = parent.find(f"sf:{child_name}", NS)
    return el.text.strip() if el is not None and el.text else None


class Finding:
    def __init__(self, severity, rule, message, node=None):
        self.severity = severity
        self.rule = rule
        self.message = message
        self.node = node

    def as_dict(self):
        return {
            "severity": self.severity,
            "rule": self.rule,
            "node": self.node,
            "message": self.message,
        }


class FlowGraph:
    """Node map plus connector edges for one parsed flow."""

    def __init__(self, root):
        self.root = root
        self.nodes = {}          # name -> {"tag": str, "el": Element}
        self.edges = {}          # name -> list[(kind, target)]
        self.start_targets = []

        for child in root:
            tag = strip_ns(child.tag)
            if tag not in NODE_TAGS:
                continue
            name = text_of(child, "name")
            if not name:
                continue
            self.nodes[name] = {"tag": tag, "el": child}
            self.edges[name] = self._connectors(child)

        start = root.find("sf:start", NS)
        if start is not None:
            self.start_targets = [t for _, t in self._connectors(start)]

    def _connectors(self, el):
        out = []
        for desc in el.iter():
            tag = strip_ns(desc.tag)
            kind = CONNECTOR_TAGS.get(tag)
            if kind is None:
                continue
            target = text_of(desc, "targetReference")
            if target:
                out.append((kind, target))
        return out

    def has_fault(self, name):
        return any(kind == "fault" for kind, _ in self.edges.get(name, []))

    def successors(self, name, include_fault=True):
        return [
            t for kind, t in self.edges.get(name, [])
            if include_fault or kind != "fault"
        ]

    def reachable(self):
        seen = set()
        stack = list(self.start_targets)
        # A flow with no <start> connector still reaches nodes via the first
        # declared element in some hand-built files; seed defensively.
        if not stack and self.nodes:
            stack = []
        while stack:
            cur = stack.pop()
            if cur in seen or cur not in self.nodes:
                continue
            seen.add(cur)
            stack.extend(self.successors(cur))
        return seen

    def loop_body_nodes(self, loop_name):
        """Nodes reachable from a loop's nextValueConnector before returning
        to the loop itself. These execute once per iteration."""
        body_entries = [
            t for kind, t in self.edges.get(loop_name, [])
            if kind == "loopBody"
        ]
        seen, stack = set(), list(body_entries)
        while stack:
            cur = stack.pop()
            if cur == loop_name or cur in seen or cur not in self.nodes:
                continue
            seen.add(cur)
            stack.extend(self.successors(cur, include_fault=False))
        return seen


def check_flow(path, api_version, story=None):
    findings = []
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        return None, [Finding("FAIL", "parse", f"Not valid XML: {exc}")]

    root = tree.getroot()
    graph = FlowGraph(root)

    process_type = text_of(root, "processType")
    status = text_of(root, "status")
    file_api = text_of(root, "apiVersion")
    description = text_of(root, "description")
    run_in_mode = text_of(root, "runInMode")
    is_screen = process_type in SCREEN_PROCESS_TYPES

    # ---- universal rules -------------------------------------------------

    if file_api != api_version:
        findings.append(Finding(
            "FAIL", "api-version",
            f"apiVersion is {file_api or 'absent'}, expected {api_version}",
        ))

    if status != "Active":
        findings.append(Finding(
            "FAIL", "status",
            f"status is {status or 'absent'}, expected Active",
        ))

    if not description:
        findings.append(Finding(
            "FAIL", "description",
            "description is absent; convention is '[story_number] — [subject]'",
        ))
    elif story and not description.startswith(story):
        findings.append(Finding(
            "WARN", "description",
            f"description does not start with {story}: {description!r}",
        ))

    # DML, queries and callouts inside loop bodies.
    for name, meta in graph.nodes.items():
        if meta["tag"] != "loops":
            continue
        for inner in sorted(graph.loop_body_nodes(name)):
            inner_tag = graph.nodes[inner]["tag"]
            if inner_tag in IO_TAGS:
                findings.append(Finding(
                    "FAIL", "io-in-loop",
                    f"{inner_tag} '{inner}' executes inside loop '{name}'; "
                    "move it outside and operate on a collection",
                    node=inner,
                ))

    # Fault handling on every node that can throw.
    for name, meta in sorted(graph.nodes.items()):
        if meta["tag"] in IO_TAGS and not graph.has_fault(name):
            findings.append(Finding(
                "FAIL", "missing-fault-path",
                f"{meta['tag']} '{name}' has no faultConnector",
                node=name,
            ))

    # Decisions must have a default outcome.
    for name, meta in sorted(graph.nodes.items()):
        if meta["tag"] != "decisions":
            continue
        has_default = any(k == "default" for k, _ in graph.edges.get(name, []))
        if not has_default:
            findings.append(Finding(
                "FAIL", "decision-no-default",
                f"decision '{name}' has no defaultConnector; records that match "
                "no rule fall through silently",
                node=name,
            ))

    # Unreachable nodes.
    reachable = graph.reachable()
    for name in sorted(graph.nodes):
        if name not in reachable:
            findings.append(Finding(
                "FAIL", "unreachable-node",
                f"{graph.nodes[name]['tag']} '{name}' is not reachable from start",
                node=name,
            ))

    # Hardcoded Salesforce Ids anywhere in element text.
    for el in root.iter():
        if not el.text:
            continue
        tag = strip_ns(el.tag)
        if tag in {"name", "targetReference", "processMetadataValues"}:
            continue
        for match in ID_CANDIDATE_RE.findall(el.text):
            if not looks_like_id(match):
                continue
            findings.append(Finding(
                "WARN", "hardcoded-id",
                f"possible hardcoded Salesforce Id {match!r} in <{tag}>; use "
                "Custom Metadata or a Custom Setting",
            ))

    # Sharing mode.
    if run_in_mode == "SystemModeWithoutSharing":
        findings.append(Finding(
            "WARN", "run-in-mode",
            "runInMode is SystemModeWithoutSharing; this bypasses sharing rules "
            "and needs explicit justification in the story",
        ))

    # ---- screen flow rules -----------------------------------------------

    if is_screen:
        screens = [n for n, m in graph.nodes.items() if m["tag"] == "screens"]
        if not screens:
            findings.append(Finding(
                "FAIL", "screen-flow-no-screens",
                "processType is Flow but the file declares no <screens>",
            ))

        if run_in_mode is None:
            findings.append(Finding(
                "WARN", "run-in-mode",
                "runInMode is absent; screen flows default to the running "
                "user's permissions, confirm that matches the AC persona",
            ))

        # Input variables that nothing consumes.
        declared_inputs = []
        for var in root.findall("sf:variables", NS):
            vname = text_of(var, "name")
            if text_of(var, "isInput") == "true" and vname:
                declared_inputs.append(vname)
        body = ET.tostring(root, encoding="unicode")
        for vname in declared_inputs:
            # One occurrence is the declaration itself.
            if body.count(vname) <= 1:
                findings.append(Finding(
                    "WARN", "unused-input-variable",
                    f"input variable '{vname}' is declared but never referenced",
                ))

        # Required-ness is visible so QA can assert it against the AC.
        for sname in sorted(screens):
            sel = graph.nodes[sname]["el"]
            for field in sel.findall("sf:fields", NS):
                fname = text_of(field, "name")
                ftype = text_of(field, "fieldType")
                if ftype == "InputField" and text_of(field, "isRequired") is None:
                    findings.append(Finding(
                        "INFO", "input-not-required",
                        f"screen '{sname}' field '{fname}' has no isRequired; "
                        "confirm against the acceptance criteria",
                        node=sname,
                    ))

    return {
        "process_type": process_type,
        "flow_type": "screen" if is_screen else (
            "autolaunched" if process_type in AUTOLAUNCHED_PROCESS_TYPES else process_type
        ),
        "status": status,
        "api_version": file_api,
        "run_in_mode": run_in_mode,
        "screen_count": sum(1 for m in graph.nodes.values() if m["tag"] == "screens"),
        "node_count": len(graph.nodes),
    }, findings


def collect_paths(inputs):
    paths = []
    for item in inputs:
        if os.path.isdir(item):
            for entry in sorted(os.listdir(item)):
                if entry.endswith(".flow-meta.xml"):
                    paths.append(os.path.join(item, entry))
        else:
            paths.append(item)
    return paths


def main():
    ap = argparse.ArgumentParser(description="Static analysis for Salesforce Flows")
    ap.add_argument("paths", nargs="+", help=".flow-meta.xml files or a directory")
    ap.add_argument("--api-version", default="67.0",
                    help="expected apiVersion (default 67.0)")
    ap.add_argument("--story", default=None,
                    help="story number, e.g. S-001042, to check the description prefix")
    ap.add_argument("--json-out", default=None, help="write full results to this path")
    args = ap.parse_args()

    paths = collect_paths(args.paths)
    if not paths:
        print("No .flow-meta.xml files found.", file=sys.stderr)
        return 2

    results, worst = [], 0
    for path in paths:
        meta, findings = check_flow(path, args.api_version, args.story)
        fails = [f for f in findings if f.severity == "FAIL"]
        warns = [f for f in findings if f.severity == "WARN"]
        if fails:
            worst = max(worst, 1)
        results.append({
            "file": path,
            "meta": meta,
            "verdict": "FAIL" if fails else ("WARN" if warns else "PASS"),
            "findings": [f.as_dict() for f in findings],
        })

    for res in results:
        head = f"{res['verdict']:5} {res['file']}"
        if res["meta"]:
            head += f"  [{res['meta']['flow_type']}, {res['meta']['node_count']} nodes]"
        print(head)
        for f in res["findings"]:
            loc = f" ({f['node']})" if f["node"] else ""
            print(f"      {f['severity']:4} {f['rule']}{loc}: {f['message']}")

    total_fail = sum(1 for r in results if r["verdict"] == "FAIL")
    print(f"\n{len(results)} flow(s) checked, {total_fail} failing.")

    if args.json_out:
        os.makedirs(os.path.dirname(args.json_out) or ".", exist_ok=True)
        with open(args.json_out, "w") as fh:
            json.dump({"results": results}, fh, indent=2)
        print(f"JSON written to {args.json_out}")

    return worst


if __name__ == "__main__":
    sys.exit(main())
