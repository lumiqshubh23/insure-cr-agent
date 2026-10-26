"""CR Agent: FRD emails -> one CR branch per product repo (backend + frontend), built in ONE Claude session.

Per CR found in the FRD mails (read from the `frd-inbox` branch of the agent repo, or Outlook via Graph):
  1. skip if NA / "no changes", if branch CR-xxxxx already exists in any product repo, or if its FRDs haven't arrived
  2. in each product repo (workspace/backend, workspace/frontend, ...): git checkout -B CR-xxxxx origin/<base>
  3. download FRDs, convert .docx -> markdown (pandoc, images kept)
  4. Claude Code pass 1 (cwd = workspace, sees all repos) -> cr-docs/CR-xxxxx/plan.md incl. API contract
  5. Claude Code pass 2 -> code + tests in whichever repos need it, cr-docs/CR-xxxxx/implementation-notes.md
  6. each repo with changes: commit plan, commit code, push CR-xxxxx. No repo changed -> plan pushed to the first repo.

Product repos are configured with REPOS, e.g.
  REPOS="backend=lumiq-ai/insure-backend@insure/develop,frontend=lumiq-ai/insure-frontend@insure/develop"
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from string import Template

from gitinbox import GitInbox
from graph import Graph
from parse import CR, branch_name, match_files, merge, parse_cr_tables

HERE = Path(__file__).resolve().parent


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Repo:
    name: str      # folder name in the workspace, e.g. "backend"
    slug: str      # owner/repo on GitHub (or a local path/URL for testing)
    base: str      # base branch, e.g. insure/develop
    path: Path = Path()


def parse_repos(spec: str, default_base: str) -> list[Repo]:
    repos = []
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        name, rest = part.split("=", 1)
        slug, base = rest.rsplit("@", 1) if "@" in rest else (rest, default_base)
        repos.append(Repo(name.strip(), slug.strip(), base.strip()))
    if not repos:
        sys.exit("REPOS is empty: e.g. REPOS='backend=org/insure-backend@insure/develop,frontend=org/insure-frontend@insure/develop'")
    return repos


CFG = {
    "agent_repo": Path(env("AGENT_REPO_DIR", ".")).resolve(),       # repo holding the frd-inbox branch
    "workspace": Path(env("WORKSPACE_DIR", "workspace")).resolve(),
    "repos": parse_repos(env("REPOS"), env("BASE_BRANCH", "insure/develop")),
    "gh_token": env("GH_TOKEN"),
    "prefix": env("BRANCH_PREFIX", ""),
    "mailbox": env("MAILBOX"),
    "search": env("SEARCH_QUERY", "FRDS"),
    # Exact subject "INSURE 2.0 Phase -2 FRDS", optionally prefixed by RE:/FW:/FWD:, any case/spacing
    "subject_regex": env("SUBJECT_REGEX", r"^\s*((re|fw|fwd)\s*:\s*)*insure\s*2\.0\s+phase\s*-\s*2\s+frds\s*$"),
    # Empty = accept any sender. Comma-separated addresses to restrict.
    "senders": [s.strip() for s in env("SENDER_FILTER", "").split(",") if s.strip()],
    "lookback": int(env("LOOKBACK_DAYS", "30")),
    "model": env("CLAUDE_MODEL"),
    "plan_turns": env("PLAN_MAX_TURNS", "50"),
    "impl_turns": env("IMPL_MAX_TURNS", "200"),
    "allowed_tools": env("ALLOWED_TOOLS", "Read,Edit,Write,Glob,Grep,Bash"),
    "disallowed_tools": env("DISALLOWED_TOOLS",
                            "Bash(git push:*),Bash(git commit:*),Bash(git checkout:*),Bash(git switch:*),"
                            "Bash(git reset:*),Bash(git rebase:*),Bash(git clean:*)"),
    # TEST_COMMAND_BACKEND, TEST_COMMAND_FRONTEND, ... -> {"backend": "...", "frontend": "..."}
    "test_cmds": {k[len("TEST_COMMAND_"):].lower(): v.strip() for k, v in os.environ.items()
                  if k.startswith("TEST_COMMAND_") and v.strip()},
}


# ---------------------------------------------------------------- git helpers
def sh(*args: str, cwd: Path, check=True, capture=False) -> subprocess.CompletedProcess:
    tok = CFG["gh_token"]
    print(f"$ ({cwd.name}) " + " ".join(a.replace(tok, "***") if tok else a for a in args), flush=True)
    return subprocess.run(args, cwd=cwd, check=check, text=True, capture_output=capture)


def clone_url(slug: str) -> str:
    if slug.startswith(("/", ".", "http", "git@", "file:")):
        return slug
    if CFG["gh_token"]:
        return f"https://x-access-token:{CFG['gh_token']}@github.com/{slug}.git"
    return f"https://github.com/{slug}.git"


def prepare_workspace() -> None:
    CFG["workspace"].mkdir(parents=True, exist_ok=True)
    for r in CFG["repos"]:
        r.path = CFG["workspace"] / r.name
        if not (r.path / ".git").exists():
            sh("git", "clone", "--no-tags", clone_url(r.slug), r.name, cwd=CFG["workspace"])
        sh("git", "config", "user.name", env("GIT_USER_NAME", "cr-agent[bot]"), cwd=r.path)
        sh("git", "config", "user.email", env("GIT_USER_EMAIL", "cr-agent@users.noreply.github.com"), cwd=r.path)
    # Workspace map for Claude (the workspace folder itself is not a git repo)
    lines = ["# CR agent workspace", "", "Product repos side by side (each is its own git repo):", ""]
    lines += [f"- `{r.name}/` -> {r.slug} (base `{r.base}`). Its conventions: `{r.name}/CLAUDE.md`."
              for r in CFG["repos"]]
    lines += ["", "A change request usually spans several repos: keep API contracts, field names, validations",
              "and error messages identical across them. Never run git commands; the pipeline handles git."]
    (CFG["workspace"] / "CLAUDE.md").write_text("\n".join(lines) + "\n")


def remote_branch_exists(repo: Repo, branch: str) -> bool:
    return bool(sh("git", "ls-remote", "--heads", "origin", branch, cwd=repo.path, capture=True).stdout.strip())


def has_changes(repo: Repo) -> bool:
    return bool(sh("git", "status", "--porcelain", cwd=repo.path, capture=True).stdout.strip())


def reset_repo(repo: Repo) -> None:
    sh("git", "reset", "--hard", cwd=repo.path, check=False)
    sh("git", "clean", "-fd", cwd=repo.path, check=False)


# ---------------------------------------------------------------- Claude helpers
def convert_frd(src: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() == ".docx":
        md = out_dir / (src.stem + ".md")
        subprocess.run(["pandoc", str(src), "-t", "gfm", "--wrap=none",
                        f"--extract-media={out_dir / (src.stem + '_media')}", "-o", str(md)], check=True)
        return md
    dst = out_dir / src.name
    shutil.copy(src, dst)
    return dst


def run_claude(prompt: str, max_turns: str, log: Path) -> dict:
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--permission-mode", "acceptEdits",
           "--allowedTools", CFG["allowed_tools"], "--disallowedTools", CFG["disallowed_tools"],
           "--max-turns", max_turns]
    if CFG["model"]:
        cmd += ["--model", CFG["model"]]
    print(f"$ claude -p <prompt> --max-turns {max_turns}", flush=True)
    r = subprocess.run(cmd, cwd=CFG["workspace"], text=True, capture_output=True, timeout=4 * 3600)
    log.write_text(f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}\n")
    try:
        result = json.loads(r.stdout)
    except json.JSONDecodeError:
        result = {"is_error": True, "result": (r.stdout or r.stderr)[-2000:]}
    if r.returncode != 0:
        result["is_error"] = True
    return result


def render(template_file: str, **values) -> str:
    return Template((HERE / "prompts" / template_file).read_text()).safe_substitute(**values)


# ---------------------------------------------------------------- per-CR pipeline
def process_cr(cr: CR, files: dict, inbox, attachments: dict) -> str:
    branch = branch_name(cr.cr_id, CFG["prefix"])
    ws = CFG["workspace"]
    work = ws / ".cr-agent" / branch                # FRDs + logs (outside every repo)
    docs = ws / "cr-docs" / branch                  # plan + notes, copied into the changed repos
    for d in (work, docs):
        shutil.rmtree(d, ignore_errors=True)
    (work / "src").mkdir(parents=True)
    docs.mkdir(parents=True)

    for r in CFG["repos"]:
        sh("git", "fetch", "origin", r.base, cwd=r.path)
        reset_repo(r)
        sh("git", "checkout", "-B", branch, f"origin/{r.base}", cwd=r.path)

    frd_paths = []
    for _, att_name in files.items():
        src = Path(inbox.download_attachment(attachments[att_name], str(work / "src" / att_name)))
        frd_paths.append(convert_frd(src, work / "frd"))

    plan_rel, notes_rel = f"cr-docs/{branch}/plan.md", f"cr-docs/{branch}/implementation-notes.md"
    tests = "\n".join(f"- in `{n}/`: `{c}`" for n, c in CFG["test_cmds"].items()) or \
        "- (not configured: find each repo's build/test/lint commands in its CLAUDE.md, pom.xml, package.json, Makefile)"
    values = dict(cr_id=cr.cr_id, summary=cr.summary, frd_date=cr.frd_date, branch=branch,
                  frd_files="\n".join(f"- {p.relative_to(ws)}" for p in frd_paths),
                  plan_path=plan_rel, notes_path=notes_rel, test_cmds=tests,
                  repo_list="\n".join(f"- `{r.name}/` ({r.slug}, cut from `{r.base}`)" for r in CFG["repos"]),
                  warnings="\n".join(f"- {w}" for w in cr.warnings) or "- none")

    res = run_claude(render("plan.md", **values), CFG["plan_turns"], work / "claude-plan.log")
    if not (ws / plan_rel).exists():
        raise RuntimeError(f"Plan pass did not write {plan_rel}: {str(res.get('result'))[:400]}")
    for r in CFG["repos"]:
        if has_changes(r):  # the plan pass must not touch code
            print(f"!! plan pass modified {r.name}; reverting")
            reset_repo(r)

    res = run_claude(render("implement.md", **values), CFG["impl_turns"], work / "claude-impl.log")

    changed = [r for r in CFG["repos"] if has_changes(r)]
    targets = changed or CFG["repos"][:1]            # nothing changed -> keep the plan in the first repo
    for r in targets:
        dst = r.path / "docs" / "cr" / branch
        dst.mkdir(parents=True, exist_ok=True)
        for f in ("plan.md", "implementation-notes.md"):
            if (docs / f).exists():
                shutil.copy(docs / f, dst / f)
        sh("git", "add", f"docs/cr/{branch}/plan.md", cwd=r.path)
        sh("git", "commit", "-m", f"{cr.cr_id}: implementation plan from FRD [cr-agent]", cwd=r.path)
        if has_changes(r):
            others = [o.name for o in changed if o is not r]
            sh("git", "add", "-A", cwd=r.path)
            sh("git", "commit", "-m",
               f"{cr.cr_id}: {cr.summary[:60]}\n\nAI-generated from FRD by cr-agent. Needs developer review before merge."
               + (f"\nCompanion changes on branch {branch} in: {', '.join(others)}" if others else ""), cwd=r.path)
        sh("git", "push", "-u", "origin", branch, cwd=r.path)

    status = ("code in " + " + ".join(r.name for r in changed)) if changed else "plan only (no code changes)"
    if res.get("is_error"):
        status += "; implementation pass ended with an error, see notes/logs"
    cost = res.get("total_cost_usd")
    return status + (f", ~${cost:.2f}" if isinstance(cost, (int, float)) and cost > 0 else "")


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default=env("ONLY_CR"))
    args = ap.parse_args()
    only = {c.strip().upper() for c in args.only.split(",") if c.strip()}
    dry = args.dry_run or env("DRY_RUN").lower() == "true"

    if env("MAIL_SOURCE", "git").lower() == "graph":
        inbox = Graph(env("MS_TENANT_ID"), env("MS_CLIENT_ID"), env("MS_CLIENT_SECRET"), CFG["mailbox"])
    else:
        inbox = GitInbox(CFG["agent_repo"], env("INBOX_BRANCH", "frd-inbox"))

    msgs = inbox.find_messages(CFG["search"], CFG["subject_regex"], CFG["senders"], CFG["lookback"])
    print(f"Found {len(msgs)} FRD email(s)")
    crs: dict[str, CR] = {}
    attachments: dict = {}
    for m in msgs:
        inbox.load_body(m)
        merge(crs, parse_cr_tables(m.html))
        for a in inbox.list_attachments(m):
            attachments[a.name] = a
    print(f"Parsed {len(crs)} CR row(s); {len(attachments)} attachment(s)")

    report = []
    if crs:
        prepare_workspace()
    for cr in crs.values():
        branch = branch_name(cr.cr_id, CFG["prefix"])
        if only and cr.cr_id.upper() not in only and branch.upper() not in only:
            continue
        if cr.skip_reason:
            report.append((cr.cr_id, "skipped", cr.skip_reason)); continue
        existing = [r.name for r in CFG["repos"] if remote_branch_exists(r, branch)]
        if existing:
            report.append((cr.cr_id, "skipped", f"branch '{branch}' already exists in {', '.join(existing)}")); continue
        files, missing = match_files(cr.files, list(attachments))
        if missing:
            report.append((cr.cr_id, "waiting", "FRD not received yet: " + "; ".join(missing))); continue
        if dry:
            report.append((cr.cr_id, "would run", f"{branch} in {', '.join(r.name for r in CFG['repos'])} <- "
                           + ", ".join(files.values()))); continue
        try:
            report.append((cr.cr_id, "done", process_cr(cr, files, inbox, attachments)))
        except Exception as e:
            print(f"!! {cr.cr_id} failed: {e}", file=sys.stderr)
            report.append((cr.cr_id, "failed", str(e)[:300]))
            for r in CFG["repos"]:
                reset_repo(r)
        finally:
            for w in cr.warnings:
                report.append((cr.cr_id, "warning", w))

    summary = "\n".join(["| CR | Result | Detail |", "|---|---|---|"] + [f"| {a} | {b} | {c} |" for a, b, c in report])
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("## CR Agent run\n\n" + summary + "\n")
    return 1 if any(r[1] == "failed" for r in report) else 0


if __name__ == "__main__":
    sys.exit(main())
