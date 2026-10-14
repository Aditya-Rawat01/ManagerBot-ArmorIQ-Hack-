"""Generate docs/Todos/STATUS.md from GitHub and the planning docs.

Usage (repository root, `gh` logged in with read access):
    python scripts/update_status.py            # rewrite docs/Todos/STATUS.md
    python scripts/update_status.py --stdout   # print only, change nothing

Sources (nothing is typed by hand, so the file cannot drift):
- docs/Todos/README.md: PR merge order with issue numbers ("### Merge sequence")
- docs/Todos/Phase-*/P?-??-*.md: task titles and "Depends on" lines (§16)
- docs/EXPERIMENTS.md: recorded experiment outcomes
- GitHub (`gh`): issue state and assignees; pull requests that say "Closes #n"

Never edit STATUS.md by hand; re-run this script instead.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TODOS = ROOT / "docs" / "Todos"
OUT = TODOS / "STATUS.md"
DEFAULT_REPO = "Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-"
TASK_ID = re.compile(r"\bP[1-5]-\d\d\b")
# UI gate (docs/Todos/README.md "Security gates"):
# P4-02 starts only after G1 (P1-06) and G3 (P1-09).
GATE_DEPS = {"P4-02": {"P1-06", "P1-09"}}
GATES = [
    ("G0", "`_ctx` + tool-server checks; backend DB role denied", ["P1-04", "P2-01"], None),
    ("G1", "E1: real call reaches MCP", ["P1-06"], "E1"),
    ("G2", "E5: policy accepted, names verified", ["P1-08"], "E5"),
    ("G3", "E3: per-member matrix", ["P1-09"], "E3"),
    ("G4", "E4: HOLD → console approval → resume", ["P1-10"], "E4"),
    ("G5", "E2: off-plan / tamper layers recorded", ["P1-07"], "E2"),
    ("G6", "E17: grounding evaluation", ["P3-08"], "E17"),
    ("G7", "Arch §13 security regression", ["P5-02"], None),
]


def gh_json(repo: str, *args: str) -> list[dict]:
    # Fixed argument list, no shell; `gh` is the documented GitHub CLI.
    out = subprocess.run(  # noqa: S603
        ["gh", *args, "-R", repo, "--state", "all", "--limit", "500"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    return json.loads(out)


def merge_waves() -> list[tuple[str, list[tuple[str, int]]]]:
    text = (TODOS / "README.md").read_text(encoding="utf-8")
    region = text[text.index("### Merge sequence") : text.index("This order was validated")]
    waves = []
    for line in region.splitlines():
        m = re.match(r"^\| (\d — [^|]+?) \| ([^|]+) \|", line)
        if m:
            pairs = re.findall(r"(P\d-\d\d) \(#(\d+)\)", m.group(2))
            waves.append((m.group(1), [(t, int(n)) for t, n in pairs]))
    if not waves:
        raise SystemExit("could not parse the merge sequence in docs/Todos/README.md")
    return waves


def task_specs() -> dict[str, dict]:
    specs = {}
    for f in sorted(TODOS.glob("Phase-*/P?-??-*.md")):
        s = f.read_text(encoding="utf-8")
        tid = f.name[:5]
        title = re.search(r"^# P\d-\d\d — (.+)$", s, re.M).group(1)
        dep = re.search(r"^- \*\*Depends on:\*\*(.+?)\*\*Blocks:\*\*", s, re.M).group(1)
        hard = set(TASK_ID.findall(re.sub(r"\([^)]*\)", "", dep))) | GATE_DEPS.get(tid, set())
        specs[tid] = {"title": title, "deps": hard, "file": f.relative_to(TODOS).as_posix()}
    return specs


def experiment_results() -> dict[str, str]:
    text = (ROOT / "docs" / "EXPERIMENTS.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\| (E\d+) \| [^|]* \| [^|]* \| ([^|]+) \|", text, re.M)
    return {eid: status.strip().replace("**", "") for eid, status in rows}


def build(repo: str) -> str:
    waves = merge_waves()
    specs = task_specs()
    issue_list = gh_json(repo, "issue", "list", "--json", "number,state,assignees")
    issues = {i["number"]: i for i in issue_list}
    prs = gh_json(
        repo, "pr", "list", "--json", "number,state,isDraft,author,closingIssuesReferences,url"
    )
    pr_for: dict[int, list[dict]] = {}
    for pr in prs:
        for ref in pr.get("closingIssuesReferences") or []:
            pr_for.setdefault(ref["number"], []).append(pr)
    issue_of = {tid: n for _, pairs in waves for tid, n in pairs}
    order = [tid for _, pairs in waves for tid, _ in pairs]

    def closed(tid: str) -> bool:
        return issues.get(issue_of.get(tid, -1), {}).get("state") == "CLOSED"

    def status(tid: str) -> tuple[str, str]:
        n = issue_of[tid]
        issue = issues.get(n)
        if issue is None:
            return "Missing", "issue not found on GitHub"
        linked = pr_for.get(n, [])
        who = ", ".join(f"`{a['login']}`" for a in issue["assignees"]) or "—"
        if issue["state"] == "CLOSED":
            merged = [p for p in linked if p["state"] == "MERGED"]
            note = ", ".join(f"PR #{p['number']}" for p in merged) or "closed without a merged PR"
            return "Done", note
        open_prs = [p for p in linked if p["state"] == "OPEN"]
        if open_prs:
            p = open_prs[0]
            kind = "Draft PR" if p["isDraft"] else "In review"
            return kind, f"PR #{p['number']} by `{p['author']['login']}`"
        waiting = sorted(d for d in specs[tid]["deps"] if not closed(d))
        if issue["assignees"]:
            refs = ", ".join(f"#{issue_of[d]}" for d in waiting)
            extra = f" — waiting on {refs}" if waiting else ""
            return "In progress", who + extra
        if waiting:
            return "Waiting", "needs " + ", ".join(f"#{issue_of[d]} ({d})" for d in waiting)
        return "Ready", "all prerequisites merged"

    rows = {tid: status(tid) for tid in order}
    counts: dict[str, int] = {}
    for st, _ in rows.values():
        counts[st] = counts.get(st, 0) + 1
    nxt = next((t for t in order if rows[t][0] != "Done"), None)
    exp = experiment_results()
    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")

    out = [
        "# Project status",
        "",
        f"_Generated {now} by `python scripts/update_status.py` from GitHub and the planning docs. "
        "Do not edit by hand; re-run the script._",
        "",
        "Legend: **Done** = issue closed by a merged PR · **In review** / **Draft PR** = open PR "
        "with `Closes #n` · **In progress** = assigned, no PR yet · **Ready** = all prerequisites "
        "merged, nobody assigned · **Waiting** = prerequisites not merged yet. "
        "Phase 3 backend work may begin against fakes before its prerequisites merge (ADR-015).",
        "",
        "## Summary",
        "",
        "| " + " | ".join(counts) + " |",
        "|" + "---|" * len(counts),
        "| " + " | ".join(str(v) for v in counts.values()) + " |",
        "",
    ]
    if nxt:
        st, note = rows[nxt]
        out += [
            f"**Next to merge:** {nxt} (#{issue_of[nxt]}) {specs[nxt]['title']} — {st}: {note}.",
            "",
        ]
        ahead = [
            t for t in order[order.index(nxt) + 1 :] if rows[t][0] in ("In review", "Draft PR")
        ]
        if ahead:
            out += [
                "**Open PRs that must wait for earlier merges:** "
                + ", ".join(f"#{issue_of[t]} ({t})" for t in ahead)
                + ".",
                "",
            ]
    else:
        out += ["**All tasks are done.**", ""]

    out += ["## Tasks in merge order", ""]
    for wave, pairs in waves:
        out += [f"### Wave {wave}", "", "| Issue | Task | Status | Details |", "|---|---|---|---|"]
        for tid, n in pairs:
            st, note = rows[tid]
            out.append(f"| #{n} | {tid} {specs[tid]['title']} | **{st}** | {note} |")
        out.append("")

    out += [
        "## Security gates",
        "",
        "A gate passes only on a real, recorded run (docs/EXPERIMENTS.md). A closed issue alone is "
        "not proof.",
        "",
        "| Gate | Check | Issues | Issue state | Recorded result |",
        "|---|---|---|---|---|",
    ]
    for gate, check, tids, eid in GATES:
        refs = ", ".join(f"#{issue_of[t]}" for t in tids)
        states = ", ".join(rows[t][0] for t in tids)
        result = exp.get(eid, "—") if eid else "—"
        out.append(f"| {gate} | {check} | {refs} | {states} | {result} |")
    out.append("")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stdout", action="store_true", help="print instead of writing STATUS.md")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    args = ap.parse_args()
    text = build(args.repo)
    if args.stdout:
        print(text)
    else:
        OUT.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {OUT.relative_to(ROOT).as_posix()}")


if __name__ == "__main__":
    main()
