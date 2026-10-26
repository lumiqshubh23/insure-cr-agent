"""CR Agent: turns FRD emails into one branch per CR, with an AI plan + implementation.

Flow per CR found in the FRD email thread:
  1. skip if NA / "no changes", if branch already exists on origin, or if its FRD files haven't arrived
  2. git checkout -B <CR> origin/<BASE_BRANCH>
  3. download FRD attachments, convert .docx -> markdown (pandoc, images extracted)
  4. Claude Code pass 1 -> docs/cr/<CR>/plan.md            (commit)
  5. Claude Code pass 2 -> code + tests + implementation-notes.md (commit)
  6. git push -u origin <CR>
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from string import Template

from graph import Graph
from parse import CR, branch_name, match_files, merge, parse_cr_tables

HERE = Path(__file__).resolve().parent


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


CFG = {
    "repo": Path(env("REPO_DIR", ".")).resolve(),
    "base": env("BASE_BRANCH", "insure/develop"),
    "prefix": env("BRANCH_PREFIX", ""),
    "mailbox": env("MAILBOX"),
    "search": env("SEARCH_QUERY", "FRDS"),
    "subject_regex": env("SUBJECT_REGEX", r"insure\s*2\.0.*phase\s*-?\s*2.*frd"),
    "senders": [s.strip() for s in env("SENDER_FILTER", "dinesh.mehta@canarahsbclife.in").split(",")],
    "lookback": int(env("LOOKBACK_DAYS", "30")),
    "model": env("CLAUDE_MODEL"),
    "plan_turns": env("PLAN_MAX_TURNS", "40"),
    "impl_turns": env("IMPL_MAX_TURNS", "150"),
    "allowed_tools": env("ALLOWED_TOOLS", "Read,Edit,Write,Glob,Grep,Bash"),
    "disallowed_tools": env("DISALLOWED_TOOLS",
                            "Bash(git push:*),Bash(git commit:*),Bash(git checkout:*),Bash(git reset:*),Bash(git rebase:*)"),
    "test_cmd": env("TEST_COMMAND"),
    "commit_frd": env("COMMIT_FRD", "false").lower() == "true",
}


# ---------------------------------------------------------------- helpers
def sh(*args: str, check=True, capture=False) -> subprocess.CompletedProcess:
    print("$", " ".join(args), flush=True)
    return subprocess.run(args, cwd=CFG["repo"], check=check, text=True,
                          capture_output=capture)


def remote_branch_exists(branch: str) -> bool:
    r = sh("git", "ls-remote", "--heads", "origin", branch, capture=True)
    return bool(r.stdout.strip())


def has_changes() -> bool:
    return bool(sh("git", "status", "--porcelain", capture=True).stdout.strip())


def ensure_excluded(path: str) -> None:
    excl = CFG["repo"] / ".git" / "info" / "exclude"
    excl.parent.mkdir(parents=True, exist_ok=True)
    lines = excl.read_text().splitlines() if excl.exists() else []
    if path not in lines:
        excl.write_text("\n".join(lines + [path]) + "\n")


def clean_tree() -> None:
    sh("git", "reset", "--hard", check=False)
    sh("git", "clean", "-fd", "-e", ".cr-agent/", check=False)


def convert_frd(src: Path, out_dir: Path) -> Path:
    """docx -> markdown with images; other formats are copied as-is for Claude to read."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() == ".docx":
        md = out_dir / (src.stem + ".md")
        media = out_dir / (src.stem + "_media")
        subprocess.run(["pandoc", str(src), "-t", "gfm", "--wrap=none",
                        f"--extract-media={media}", "-o", str(md)], check=True)
        return md
    dst = out_dir / src.name
    shutil.copy(src, dst)
    return dst


def run_claude(prompt: str, max_turns: str, log: Path) -> dict:
    cmd = ["claude", "-p", prompt,
           "--output-format", "json",
           "--permission-mode", "acceptEdits",
           "--allowedTools", CFG["allowed_tools"],
           "--disallowedTools", CFG["disallowed_tools"],
           "--max-turns", max_turns]
    if CFG["model"]:
        cmd += ["--model", CFG["model"]]
    print(f"$ claude -p <prompt> --max-turns {max_turns}", flush=True)
    r = subprocess.run(cmd, cwd=CFG["repo"], text=True, capture_output=True, timeout=3 * 3600)
    log.write_text(f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}\n")
    try:
        result = json.loads(r.stdout)
    except json.JSONDecodeError:
        result = {"is_error": True, "result": r.stdout[-2000:] or r.stderr[-2000:]}
    if r.returncode != 0:
        result["is_error"] = True
    return result


def render(template_file: str, **values) -> str:
    return Template((HERE / "prompts" / template_file).read_text()).safe_substitute(**values)


# ---------------------------------------------------------------- per-CR pipeline
def process_cr(cr: CR, files: dict, graph: Graph, attachments: dict) -> str:
    branch = branch_name(cr.cr_id, CFG["prefix"])
    work = CFG["repo"] / ".cr-agent" / branch
    ensure_excluded(".cr-agent/")
    shutil.rmtree(work, ignore_errors=True)
    (work / "src").mkdir(parents=True)

    sh("git", "fetch", "origin", CFG["base"])
    sh("git", "checkout", "-B", branch, f"origin/{CFG['base']}")

    # download + convert FRDs
    frd_paths = []
    for expected, att_name in files.items():
        src = Path(graph.download_attachment(attachments[att_name], str(work / "src" / att_name)))
        frd_paths.append(convert_frd(src, work / "frd"))
    if CFG["commit_frd"]:
        dst = CFG["repo"] / "docs" / "cr" / branch / "frd"
        shutil.copytree(work / "frd", dst, dirs_exist_ok=True)
        frd_paths = [dst / p.name for p in frd_paths]
    rel_frds = "\n".join(f"- {p.relative_to(CFG['repo'])}" for p in frd_paths)

    plan_rel = f"docs/cr/{branch}/plan.md"
    notes_rel = f"docs/cr/{branch}/implementation-notes.md"
    values = dict(cr_id=cr.cr_id, summary=cr.summary, frd_date=cr.frd_date, frd_files=rel_frds,
                  plan_path=plan_rel, notes_path=notes_rel, base_branch=CFG["base"], branch=branch,
                  warnings="\n".join(f"- {w}" for w in cr.warnings) or "- none",
                  test_cmd=CFG["test_cmd"] or "(not configured: find the project's test/lint commands yourself, e.g. in CLAUDE.md, package.json, pom.xml, Makefile)")

    # pass 1: plan
    res = run_claude(render("plan.md", **values), CFG["plan_turns"], work / "claude-plan.log")
    if not (CFG["repo"] / plan_rel).exists():
        raise RuntimeError(f"Plan pass did not write {plan_rel}: {str(res.get('result'))[:500]}")
    sh("git", "add", "-A")
    sh("git", "commit", "-m", f"{cr.cr_id}: implementation plan from FRD [cr-agent]")

    # pass 2: implement
    res = run_claude(render("implement.md", **values), CFG["impl_turns"], work / "claude-impl.log")
    status = "implemented"
    if res.get("is_error"):
        status = "partial (implementation pass ended with an error; see notes/logs)"
    if has_changes():
        sh("git", "add", "-A")
        sh("git", "commit", "-m",
           f"{cr.cr_id}: {cr.summary[:60]}\n\nAI-generated from FRD by cr-agent. Needs developer review before merge.")
    else:
        status = "plan only (implementation pass made no code changes)"

    sh("git", "push", "-u", "origin", branch)
    cost = res.get("total_cost_usd")
    return status + (f", ~${cost:.2f}" if isinstance(cost, (int, float)) else "")


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="only list CRs and what would happen")
    ap.add_argument("--only", default=env("ONLY_CR"), help="comma-separated CR ids to process")
    args = ap.parse_args()
    only = {c.strip().upper() for c in args.only.split(",") if c.strip()}
    dry = args.dry_run or env("DRY_RUN").lower() == "true"

    graph = Graph(env("MS_TENANT_ID"), env("MS_CLIENT_ID"), env("MS_CLIENT_SECRET"), CFG["mailbox"])
    msgs = graph.find_messages(CFG["search"], CFG["subject_regex"], CFG["senders"], CFG["lookback"])
    print(f"Found {len(msgs)} FRD email(s)")

    crs: dict[str, CR] = {}
    attachments: dict = {}
    for m in msgs:
        graph.load_body(m)
        merge(crs, parse_cr_tables(m.html))
        for a in graph.list_attachments(m):
            attachments[a.name] = a  # later emails win on same name
    print(f"Parsed {len(crs)} CR row(s); {len(attachments)} attachment(s) in thread")

    report = []
    for cr in crs.values():
        branch = branch_name(cr.cr_id, CFG["prefix"])
        if only and cr.cr_id.upper() not in only and branch.upper() not in only:
            continue
        if cr.skip_reason:
            report.append((cr.cr_id, "skipped", cr.skip_reason)); continue
        if remote_branch_exists(branch):
            report.append((cr.cr_id, "skipped", f"branch '{branch}' already exists")); continue
        files, missing = match_files(cr.files, list(attachments))
        if missing:
            report.append((cr.cr_id, "waiting", "FRD not received yet: " + "; ".join(missing))); continue
        if dry:
            report.append((cr.cr_id, "would run", f"branch {branch} <- {', '.join(files.values())}")); continue
        try:
            report.append((cr.cr_id, "done", process_cr(cr, files, graph, attachments)))
        except Exception as e:  # keep going with the next CR
            print(f"!! {cr.cr_id} failed: {e}", file=sys.stderr)
            report.append((cr.cr_id, "failed", str(e)[:300]))
            clean_tree()
        finally:
            for w in cr.warnings:
                report.append((cr.cr_id, "warning", w))

    lines = ["| CR | Result | Detail |", "|---|---|---|"] + [f"| {a} | {b} | {c} |" for a, b, c in report]
    summary = "\n".join(lines)
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("## CR Agent run\n\n" + summary + "\n")
    return 1 if any(r[1] == "failed" for r in report) else 0


if __name__ == "__main__":
    sys.exit(main())
