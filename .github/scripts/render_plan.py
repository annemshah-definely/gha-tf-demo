#!/usr/bin/env python3
"""
render_plan.py - render a Terraform plan (and optionally an apply) as an
HCP Terraform-style run panel in GitHub-flavoured Markdown.

The panel mimics the HCP Terraform workspace / run page:

  * a header table (workspace, status, change counts, run, commit, actor)
  * the run phases with their outcome (init / fmt / validate / plan / apply)
  * "Resource changes" - one collapsible row per resource, colour-coded by
    action (create / update / replace / destroy / read / import ...), which
    expands into the resource diff rendered as a ```diff block so GitHub
    colours additions green, removals red and in-place updates orange
  * output changes, drift detected outside Terraform, warnings and errors

Inputs (all optional - the script degrades gracefully when files are missing):

  --plan-json   `terraform show -json <planfile>`      metadata: actions, reasons, drift
  --plan-txt    `terraform show -no-color <planfile>`  the human diff, split per resource
  --run-log     combined output of init / validate / plan (errors & warnings)
  --fmt-diff    output of `terraform fmt -check -diff`
  --apply-log   output of `terraform apply`
  --stage       name=outcome (repeatable). Outcomes are GitHub step outcomes:
                success | failure | skipped | cancelled

Outputs:

  --comment-out  Markdown sized for a PR comment (<= 60k chars, diffs truncated)
  --summary-out  Markdown sized for $GITHUB_STEP_SUMMARY (<= 1 MiB, full diffs)

When $GITHUB_OUTPUT is set, `status=<key>` and `headline=<text>` are appended.

Only the Python standard library is used.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# --------------------------------------------------------------------------- limits
COMMENT_LIMIT = 60_000          # GitHub PR comments max out at 65,536 characters
SUMMARY_LIMIT = 1_000_000       # $GITHUB_STEP_SUMMARY max is 1 MiB
COMMENT_RESOURCE_LINES = 120    # diff lines per resource in the comment
SUMMARY_RESOURCE_LINES = 2_000  # diff lines per resource in the job summary
LOG_TAIL_LINES = 80
MAX_COMPACT_ROWS = 400

# --------------------------------------------------------------------------- patterns
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
HEADER_RE = re.compile(r"^ {0,3}# (?!\()(?P<rest>\S.*?)\s*$")
ADDR_VERB_RE = re.compile(r"^(?P<addr>.+?) (?P<verb>(?:will|must|has|is) .*)$")
SIGIL_RE = re.compile(r"^(?P<indent> *)(?P<sigil>-/\+|\+/-|<=|[-+~])(?P<rest> .*)$")
COMMENT_RE = re.compile(r"^(?P<indent> *)#(?P<rest>.*)$")
APPLY_DONE_RE = re.compile(r"^Apply complete! Resources: (?P<detail>.*?)\.?\s*$", re.M)

SIGIL_MAP = {"+": "+", "-": "-", "~": "!"}

# --------------------------------------------------------------------------- taxonomy
# action key -> (colour emoji, HCP-style label)
ACTIONS = {
    "create":  ("🟢", "create"),
    "update":  ("🟡", "update in-place"),
    "replace": ("🟠", "replace"),
    "delete":  ("🔴", "destroy"),
    "read":    ("🔵", "read"),
    "import":  ("🟣", "import"),
    "move":    ("⚪", "move"),
    "forget":  ("⚪", "forget"),
}

# text-plan header verbs -> action key (used only when plan.json is unavailable)
VERB_ACTIONS = [
    ("will be created", "create"),
    ("will be updated in-place", "update"),
    ("will be destroyed", "delete"),
    ("must be replaced", "replace"),
    ("will be replaced", "replace"),
    ("is tainted", "replace"),
    ("will be read during apply", "read"),
    ("will be imported", "import"),
    ("has moved to", "move"),
    ("will no longer be managed", "forget"),
]

# terraform show -json `action_reason` -> human qualifier
REASONS = {
    "replace_because_tainted": "tainted",
    "replace_by_request": "replace requested with -replace",
    "replace_because_triggered_by": "replace_triggered_by",
    "delete_because_no_resource_config": "no longer in configuration",
    "delete_because_wrong_repetition": "repetition mode changed",
    "delete_because_count_index": "count index out of range",
    "delete_because_each_key": "for_each key removed",
    "delete_because_no_module": "module removed",
    "delete_because_no_move_target": "moved block has no target",
    "read_because_config_unknown": "config refers to values not yet known",
    "read_because_dependency_pending": "depends on pending changes",
    "read_because_check_nested": "nested in a check block",
}

STATUS = {
    "planned":       ("🟢", "Planned"),
    "planned-fmt":   ("🟠", "Planned · formatting check failed"),
    "no-changes":    ("⚪", "No changes"),
    "errored":       ("🔴", "Errored"),
    "pending-apply": ("🟡", "Planned · awaiting apply"),
    "applied":       ("🟢", "Applied"),
    "apply-errored": ("🔴", "Apply errored"),
}

OUTCOME_ICON = {"success": "✅", "failure": "❌", "skipped": "⏭️", "cancelled": "🚫"}
STAGE_ORDER = ["init", "fmt", "validate", "plan", "apply"]


# --------------------------------------------------------------------------- data
@dataclass
class Resource:
    address: str
    action: str
    reason: str = ""
    importing: bool = False
    lines: list = field(default_factory=list)   # raw text-plan lines (header included)


@dataclass
class TextPlan:
    sections: dict = field(default_factory=dict)      # address -> lines
    order: list = field(default_factory=list)
    verbs: dict = field(default_factory=dict)         # address -> verb phrase
    drift: dict = field(default_factory=dict)         # address -> lines
    drift_order: list = field(default_factory=list)
    outputs: list = field(default_factory=list)
    plan_line: str = ""
    no_changes: bool = False


@dataclass
class Doc:
    mode: str
    environment: str
    working_directory: str
    stages: dict
    server_url: str = ""
    repository: str = ""
    run_id: str = ""
    run_number: str = ""
    run_attempt: str = ""
    commit: str = ""
    actor: str = ""
    pr: str = ""
    plan_json: Optional[dict] = None
    text: Optional[TextPlan] = None
    resources: list = field(default_factory=list)
    output_changes: int = 0
    drift_json: list = field(default_factory=list)
    diagnostics: list = field(default_factory=list)   # (kind, title, lines)
    run_log: str = ""
    fmt_diff: str = ""
    apply_log: str = ""
    apply_detail: str = ""
    status: str = "errored"


# --------------------------------------------------------------------------- helpers
def read_text(path: Optional[str]) -> str:
    if not path or not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8", errors="replace") as fh:
        return ANSI_RE.sub("", fh.read())


def read_json(path: Optional[str]) -> Optional[dict]:
    if not path or not os.path.isfile(path) or os.path.getsize(path) == 0:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def short_sha(sha: str) -> str:
    return sha[:7] if sha else ""


def path_str(path) -> str:
    out = ""
    for seg in path:
        out += f"[{seg}]" if isinstance(seg, int) else (f".{seg}" if out else str(seg))
    return out


def tail(text: str, n: int) -> list:
    lines = text.rstrip("\n").splitlines()
    return lines[-n:] if len(lines) > n else lines


# --------------------------------------------------------------------------- parsing: text plan
def parse_plan_text(text: str) -> TextPlan:
    tp = TextPlan()
    region = "preamble"
    current: Optional[list] = None

    def close():
        nonlocal current
        if current is not None:
            while current and not current[-1].strip():
                current.pop()
        current = None

    for line in text.splitlines():
        if region == "outputs":
            if not line.strip() or line.startswith(" "):
                tp.outputs.append(line)
                continue
            region = "post"

        if "Objects have changed outside of Terraform" in line:
            close(); region = "drift"; continue
        if line.startswith(("Terraform will perform the following actions",
                            "Terraform planned the following actions")):
            close(); region = "actions"; continue
        if line.startswith("Changes to Outputs:"):
            close(); region = "outputs"; continue
        if line.startswith("Plan:"):
            close(); tp.plan_line = line.strip(); region = "post"; continue
        if line.startswith("No changes."):
            close(); tp.no_changes = True; region = "post"; continue

        if region in ("drift", "actions"):
            m = HEADER_RE.match(line)
            if m:
                close()
                rest = m["rest"]
                am = ADDR_VERB_RE.match(rest)
                addr, verb = (am["addr"], am["verb"]) if am else (rest, "")
                bucket, order = (tp.sections, tp.order) if region == "actions" else (tp.drift, tp.drift_order)
                bucket[addr] = [line]
                order.append(addr)
                tp.verbs[addr] = verb
                current = bucket[addr]
                continue
            if current is not None:
                # un-indented prose ("Unless you have made equivalent changes...") ends a section;
                # `-/+ resource` and `+/- resource` start in column 0 and belong to the section.
                if line and not line.startswith((" ", "-/+", "+/-")):
                    close()
                else:
                    current.append(line)
    close()
    while tp.outputs and not tp.outputs[-1].strip():
        tp.outputs.pop()
    return tp


def parse_diagnostics(text: str) -> list:
    """Return [(kind, title, lines)] for every ╷ ... ╵ box Terraform printed."""
    boxes, cur = [], None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("╷"):
            cur = []
            continue
        if s.startswith("╵"):
            if cur is not None:
                body = [l for l in cur]
                while body and not body[-1].strip():
                    body.pop()
                first = body[0] if body else ""
                kind = "error" if first.startswith("Error") else "warning" if first.startswith("Warning") else "note"
                title = first.split(":", 1)[1].strip() if ":" in first else first
                boxes.append((kind, title, body))
            cur = None
            continue
        if cur is not None:
            cur.append(line[2:] if line.startswith("│ ") else line.lstrip("│"))
    return boxes


# --------------------------------------------------------------------------- parsing: json plan
def classify(rc: dict):
    change = rc.get("change") or {}
    actions = change.get("actions") or []
    importing = bool(change.get("importing"))
    moved = rc.get("previous_address")
    qual = []

    if actions == ["no-op"]:
        if importing:
            key = "import"
        elif moved:
            key = "move"
        else:
            return None, "", importing
    elif actions == ["create"]:
        key = "create"
    elif actions == ["update"]:
        key = "update"
    elif actions == ["delete"]:
        key = "delete"
    elif sorted(actions) == ["create", "delete"]:
        key = "replace"
    elif actions == ["read"]:
        key = "read"
    elif actions == ["forget"]:
        key = "forget"
    else:
        key = "update"

    if importing and key != "import":
        qual.append("imported")
    if moved and key != "move":
        qual.append(f"moved from {moved}")
    if key == "replace":
        paths = change.get("replace_paths") or []
        if paths:
            qual.append("forces replacement: " + ", ".join(path_str(p) for p in paths))
        elif rc.get("action_reason") == "replace_because_cannot_update":
            qual.append("cannot update in place")
    reason = REASONS.get(rc.get("action_reason") or "")
    if reason:
        qual.append(reason)
    if rc.get("deposed"):
        qual.append("deposed object")
    return key, "; ".join(qual), importing


def build_resources(plan_json: Optional[dict], tp: TextPlan) -> list:
    resources = []
    if plan_json is not None:
        for rc in plan_json.get("resource_changes") or []:
            key, reason, importing = classify(rc)
            if key is None:
                continue
            addr = rc.get("address", "")
            lookup = f"{addr} (deposed object {rc['deposed']})" if rc.get("deposed") else addr
            lines = tp.sections.get(lookup) or tp.sections.get(addr) or []
            resources.append(Resource(lookup, key, reason, importing, lines))
        return resources
    for addr in tp.order:   # no json: fall back to the text plan only
        verb = tp.verbs.get(addr, "")
        key = next((k for v, k in VERB_ACTIONS if verb.startswith(v)), "update")
        resources.append(Resource(addr, key, "", False, tp.sections[addr]))
    return resources


def count(resources: list) -> dict:
    c = {k: 0 for k in ACTIONS}
    for r in resources:
        c[r.action] += 1
    return {
        "add": c["create"] + c["replace"],
        "change": c["update"],
        "destroy": c["delete"] + c["replace"],
        "replace": c["replace"],
        "import": sum(1 for r in resources if r.importing),
        "read": c["read"],
        "move": c["move"],
        "forget": c["forget"],
    }


# --------------------------------------------------------------------------- diff rendering
def to_diff(lines: list, dedent: int = 2) -> list:
    """Move Terraform's change sigils into column 0 so GitHub's ```diff highlighting applies:
    `+` green, `-` red, `~` -> `!` orange, `#` grey. Indentation is preserved relative to
    the resource block so the output still reads like Terraform's own."""
    out = []
    for line in lines:
        m = SIGIL_RE.match(line)
        if m:
            indent = " " * max(0, len(m["indent"]) - dedent)
            sigil, rest = m["sigil"], m["rest"]
            if sigil == "-/+":
                out.append(f"!{indent}{rest.rstrip()}   # -/+ destroy and then create replacement")
            elif sigil == "+/-":
                out.append(f"!{indent}{rest.rstrip()}   # +/- create replacement and then destroy")
            elif sigil == "<=":
                out.append(f"#{indent}{rest.rstrip()}   # <= read during apply")
            else:
                out.append(f"{SIGIL_MAP[sigil]}{indent}{rest}")
            continue
        m = COMMENT_RE.match(line)
        if m:
            out.append("#" + " " * max(0, len(m["indent"]) - dedent) + m["rest"])
            continue
        out.append(line[dedent:] if line.startswith(" " * dedent) else line)
    return out


def truncate(lines: list, limit: int, hint: str) -> list:
    if len(lines) <= limit:
        return lines
    return lines[:limit] + [f"# … {len(lines) - limit} more lines truncated — {hint}"]


def fence(lines: list, lang: str = "diff") -> str:
    return f"````{lang}\n" + "\n".join(lines) + "\n````"


def details(summary: str, body: str, open_: bool = False) -> str:
    tag = "<details open>" if open_ else "<details>"
    return f"{tag}\n<summary>{summary}</summary>\n\n{body}\n\n</details>"


def row_summary(r: Resource) -> str:
    emoji, label = ACTIONS[r.action]
    qual = f" <sub>{esc(r.reason)}</sub>" if r.reason else ""
    return f"{emoji} <code>{esc(r.address)}</code> <kbd>{label}</kbd>{qual}"


# --------------------------------------------------------------------------- document
def compute_status(doc: Doc) -> str:
    st = doc.stages
    if st.get("apply") == "success":
        return "applied"
    if st.get("apply") in ("failure", "cancelled"):
        return "apply-errored"
    if any(st.get(s) in ("failure", "cancelled") for s in ("init", "validate", "plan")):
        return "errored"
    if doc.plan_json is None and doc.text is None:
        return "errored"
    no_changes = (not doc.resources and not doc.output_changes) or (doc.text is not None and doc.text.no_changes)
    if no_changes:
        return "no-changes"
    if doc.mode == "apply":
        return "pending-apply"
    if st.get("fmt") == "failure":
        return "planned-fmt"
    return "planned"


def build_doc(args) -> Doc:
    stages = {}
    for item in args.stage or []:
        name, _, outcome = item.partition("=")
        if name and outcome:
            stages[name.strip()] = outcome.strip()

    doc = Doc(
        mode=args.mode, environment=args.environment, working_directory=args.working_directory,
        stages=stages, server_url=args.server_url.rstrip("/"), repository=args.repository,
        run_id=args.run_id, run_number=args.run_number, run_attempt=args.run_attempt,
        commit=args.commit, actor=args.actor, pr=args.pr,
    )
    doc.plan_json = read_json(args.plan_json)
    plan_txt = read_text(args.plan_txt)
    doc.text = parse_plan_text(plan_txt) if plan_txt.strip() else None
    doc.run_log = read_text(args.run_log)
    doc.fmt_diff = read_text(args.fmt_diff)
    doc.apply_log = read_text(args.apply_log)

    tp = doc.text or TextPlan()
    doc.resources = build_resources(doc.plan_json, tp)
    if doc.plan_json is not None:
        doc.output_changes = sum(
            1 for oc in (doc.plan_json.get("output_changes") or {}).values()
            if (oc.get("actions") or []) != ["no-op"]
        )
        doc.drift_json = doc.plan_json.get("resource_drift") or []
    elif tp.outputs:
        doc.output_changes = sum(1 for l in tp.outputs if SIGIL_RE.match(l))

    seen = set()
    for src in (doc.run_log, plan_txt, doc.apply_log):
        for box in parse_diagnostics(src):
            sig = "\n".join(box[2])
            if sig not in seen:
                seen.add(sig)
                doc.diagnostics.append(box)

    m = APPLY_DONE_RE.search(doc.apply_log)
    if m:
        doc.apply_detail = m["detail"]
    doc.status = compute_status(doc)
    return doc


# --------------------------------------------------------------------------- markdown
def render(doc: Doc, *, limit: int, resource_lines: int, is_comment: bool) -> str:
    emoji, status_text = STATUS[doc.status]
    counts = count(doc.resources)
    run_url = f"{doc.server_url}/{doc.repository}/actions/runs/{doc.run_id}" if doc.run_id else ""
    hint = "open the job summary for the full diff" if is_comment else "see the raw plan in the job log"
    kind = "plan" if doc.mode == "plan" else "run"
    title_kind = "Terraform plan" if doc.mode == "plan" else "Terraform run"

    # ---- header ---------------------------------------------------------------
    head = [f"<!-- terraform-{kind}:{doc.environment} -->", f"## {emoji} {title_kind} · {doc.environment}", ""]

    if doc.status == "applied" and doc.apply_detail:
        changes = esc(doc.apply_detail)
    elif doc.status in ("no-changes", "errored"):
        changes = "—"
    else:
        changes = f"<code>+{counts['add']}</code> <code>~{counts['change']}</code> <code>-{counts['destroy']}</code>"
        if counts["import"]:
            changes += f" <code>↓{counts['import']}</code>"

    run_cell = f"[#{doc.run_number}]({run_url})" if run_url and doc.run_number else "—"
    if doc.run_attempt and doc.run_attempt not in ("", "1"):
        run_cell += f" <sub>attempt {doc.run_attempt}</sub>"
    commit_cell = (f"[`{short_sha(doc.commit)}`]({doc.server_url}/{doc.repository}/commit/{doc.commit})"
                   if doc.commit and doc.repository else "—")
    if doc.pr:
        commit_cell += f" <sub>PR #{doc.pr}</sub>"
    actor_cell = f"[{esc(doc.actor)}]({doc.server_url}/{doc.actor})" if doc.actor else "—"

    head += [
        "| Workspace | Status | Changes | Run | Commit | Triggered by |",
        "|:--|:--|:--|:--|:--|:--|",
        f"| **{esc(doc.environment)}**<br><sub><code>{esc(doc.working_directory)}</code></sub> "
        f"| {emoji} **{status_text}** | {changes} | {run_cell} | {commit_cell} | {actor_cell} |",
        "",
    ]
    stage_bits = [f"{OUTCOME_ICON.get(doc.stages[s], '▫️')} {s}" for s in STAGE_ORDER if s in doc.stages]
    if stage_bits:
        head += ["<sub>" + " &nbsp;·&nbsp; ".join(stage_bits) + "</sub>", ""]

    # ---- body -----------------------------------------------------------------
    body_top, rows_full, rows_compact, body_bottom = [], [], [], []

    if doc.status in ("errored", "apply-errored"):
        errors = [d for d in doc.diagnostics if d[0] == "error"]
        failed = [s for s in STAGE_ORDER if doc.stages.get(s) in ("failure", "cancelled")]
        body_top.append(f"### ❌ {('`terraform ' + failed[0] + '` failed') if failed else 'The run failed'}")
        body_top.append("")
        for _, title, lines in errors[:10]:
            body_top.append(f"**Error: {esc(title)}**")
            body_top.append("")
            body_top.append(fence(truncate(lines, 60, hint), "text"))
            body_top.append("")
        log_src = doc.apply_log if doc.status == "apply-errored" else doc.run_log
        if log_src.strip():
            body_top.append(details(f"🧾 Raw log (last {LOG_TAIL_LINES} lines)",
                                    fence(tail(log_src, LOG_TAIL_LINES), "text")))
            body_top.append("")

    if doc.status == "no-changes":
        body_top += ["> ⚪ **No changes.** Your infrastructure matches the configuration.", ""]

    if doc.resources:
        heading = "Resources applied" if doc.status == "applied" else "Resource changes"
        chips = [f"🟢 **{counts['add']} to add**", f"🟡 **{counts['change']} to change**",
                 f"🔴 **{counts['destroy']} to destroy**"]
        if counts["replace"]:
            chips.append(f"🟠 **{counts['replace']} to replace**")
        if counts["import"]:
            chips.append(f"🟣 **{counts['import']} to import**")
        if counts["read"]:
            chips.append(f"🔵 **{counts['read']} to read**")
        if counts["move"]:
            chips.append(f"⚪ **{counts['move']} moved**")
        if counts["forget"]:
            chips.append(f"⚪ **{counts['forget']} forgotten**")
        body_top += [f"### {heading}", "", " &nbsp;·&nbsp; ".join(chips), ""]

        for r in doc.resources:
            summary = row_summary(r)
            if r.lines:
                block = fence(truncate(to_diff(r.lines), resource_lines, hint))
                rows_full.append(details(summary, block))
            else:
                rows_full.append(f"{summary} <sub>(no diff available)</sub>\n")
            rows_compact.append(f"- {summary}")

    # ---- bottom sections --------------------------------------------------------
    if doc.status == "applied" and doc.apply_log.strip():
        body_bottom.append(details(f"🧾 Apply output (last {LOG_TAIL_LINES} lines)",
                                   fence(tail(doc.apply_log, LOG_TAIL_LINES), "text")))
        body_bottom.append("")

    if doc.text and doc.text.outputs and doc.status not in ("errored", "apply-errored"):
        body_bottom.append(details(f"📤 Output changes <sub>{doc.output_changes}</sub>",
                                   fence(truncate(to_diff(doc.text.outputs, dedent=2), resource_lines, hint))))
        body_bottom.append("")

    drift_count = len(doc.drift_json) or (len(doc.text.drift_order) if doc.text else 0)
    if drift_count:
        parts = []
        if doc.text and doc.text.drift_order:
            for addr in doc.text.drift_order:
                parts.append(fence(truncate(to_diff(doc.text.drift[addr]), resource_lines, hint)))
        else:
            parts.append("\n".join(f"- <code>{esc(d.get('address', ''))}</code>" for d in doc.drift_json))
        body_bottom.append(details(f"⚠️ Drift · {drift_count} object(s) changed outside of Terraform",
                                   "\n\n".join(parts)))
        body_bottom.append("")

    warnings = [d for d in doc.diagnostics if d[0] == "warning"]
    if warnings:
        parts = []
        for _, title, lines in warnings[:20]:
            parts.append(f"**Warning: {esc(title)}**\n\n" + fence(truncate(lines, 40, hint), "text"))
        body_bottom.append(details(f"⚠️ {len(warnings)} warning(s)", "\n\n".join(parts)))
        body_bottom.append("")

    if doc.stages.get("fmt") == "failure":
        diff = doc.fmt_diff.strip() or "terraform fmt -check reported unformatted files (no diff captured)."
        body_bottom.append(details("🧹 Formatting check failed — run <code>terraform fmt -recursive</code>",
                                   fence(truncate(diff.splitlines(), 200, hint))))
        body_bottom.append("")

    footer_bits = []
    if doc.plan_json:
        if doc.plan_json.get("terraform_version"):
            footer_bits.append(f"Terraform v{esc(str(doc.plan_json['terraform_version']))}")
        if doc.plan_json.get("timestamp"):
            footer_bits.append(f"planned {esc(str(doc.plan_json['timestamp']))}")
    footer_bits.append("rendered " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    if run_url:
        footer_bits.append(f"<a href=\"{run_url}\">full plan &amp; logs</a>")
    body_bottom.append("<sub>" + " · ".join(footer_bits) + "</sub>")

    # ---- assemble within budget ---------------------------------------------------
    # Expand resources (full <details> diff) in plan order while the whole document still fits;
    # everything after the cut-off becomes a one-line compact row. Compact rows are budgeted too.
    fixed = "\n".join(head + body_top) + "\n" + "\n".join(body_bottom)
    budget = limit - len(fixed) - 600
    compact_cost = [len(r) + 1 for r in rows_compact]
    suffix = [0] * (len(rows_compact) + 1)
    for i in range(len(rows_compact) - 1, -1, -1):
        suffix[i] = suffix[i + 1] + compact_cost[i]

    rows, used, expanded = [], 0, 0
    for i, block in enumerate(rows_full):
        if used + len(block) + 1 + suffix[i + 1] <= budget:
            rows.append(block)
            used += len(block) + 1
            expanded = i + 1
        else:
            break

    rest = rows_compact[expanded:]
    if rest:
        rows.append("")
        shown = 0
        for row, cost in zip(rest, compact_cost[expanded:]):
            if shown >= MAX_COMPACT_ROWS or used + cost > budget:
                break
            rows.append(row)
            used += cost
            shown += 1
        if shown < len(rest):
            rows.append(f"- … {len(rest) - shown} more")
        rows.append("")
        rows.append(f"> ✂️ {len(rest)} resource diff(s) collapsed to fit this comment — "
                    + (f"[open the job summary]({run_url}) for the full plan." if run_url else "see the job summary."))

    out = "\n".join(head + body_top + rows + [""] + body_bottom) + "\n"
    out = re.sub(r"\n{3,}", "\n\n", out)
    if len(out) > limit:    # last resort - never exceed the hard limit
        out = out[: limit - 80].rstrip() + "\n\n> ✂️ truncated — open the job summary for the full plan.\n"
    return out


# --------------------------------------------------------------------------- main
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=["plan", "apply"], default="plan",
                   help="plan: speculative plan on a PR. apply: plan+apply on the default branch")
    p.add_argument("--environment", required=True)
    p.add_argument("--working-directory", default="")
    p.add_argument("--plan-json")
    p.add_argument("--plan-txt")
    p.add_argument("--run-log")
    p.add_argument("--fmt-diff")
    p.add_argument("--apply-log")
    p.add_argument("--stage", action="append", help="name=outcome (init|fmt|validate|plan|apply)")
    p.add_argument("--server-url", default="https://github.com")
    p.add_argument("--repository", default="")
    p.add_argument("--run-id", default="")
    p.add_argument("--run-number", default="")
    p.add_argument("--run-attempt", default="")
    p.add_argument("--commit", default="")
    p.add_argument("--actor", default="")
    p.add_argument("--pr", default="")
    p.add_argument("--comment-out")
    p.add_argument("--summary-out")
    args = p.parse_args()

    doc = build_doc(args)
    comment = render(doc, limit=COMMENT_LIMIT, resource_lines=COMMENT_RESOURCE_LINES, is_comment=True)
    summary = render(doc, limit=SUMMARY_LIMIT, resource_lines=SUMMARY_RESOURCE_LINES, is_comment=False)

    if args.comment_out:
        with open(args.comment_out, "w", encoding="utf-8") as fh:
            fh.write(comment)
    if args.summary_out:
        with open(args.summary_out, "w", encoding="utf-8") as fh:
            fh.write(summary)
    if not args.comment_out and not args.summary_out:
        print(summary)

    emoji, text = STATUS[doc.status]
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"status={doc.status}\nheadline={emoji} {text}\n")
    print(f"{emoji} {doc.environment}: {text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
