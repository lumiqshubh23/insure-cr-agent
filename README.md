# CR Agent: FRD email → one branch per CR

When Dinesh Mehta sends an **"INSURE 2.0 Phase -2 FRDS"** email, this agent:

1. Reads the CR table in the email (and its RE:/FW: follow-ups) and collects every FRD attachment in the thread.
2. For each CR, skips it if it's marked **NA / "No changes in INSURE2.0"**, if its branch already exists, or if its FRD hasn't arrived yet (it is picked up automatically on a later run).
3. Cuts a branch named after the CR (`CR-18954`, `SIRS-117`, ...) from `origin/insure/develop`.
4. Converts the FRD .docx to markdown (images kept) and runs **Claude Code** twice:
   - **Plan pass** → `docs/cr/<CR>/plan.md` (requirement checklist REQ-1..n, impacted areas, test plan, open questions). Committed.
   - **Implement pass** → code + tests, and `docs/cr/<CR>/implementation-notes.md` (REQ status, assumptions, test results). Committed.
5. Pushes the branch. No PR, no merge: a developer reviews the branch and raises the PR.

The run summary (done / waiting / skipped / warnings, e.g. FRD file named under a different CR) appears on the GitHub Actions run page.

## Files

```
.github/workflows/cr-agent.yml   # trigger: Power Automate ping, 30-min poll, or manual
tools/cr-agent/main.py           # orchestration + git
tools/cr-agent/graph.py          # Outlook access via Microsoft Graph
tools/cr-agent/parse.py          # CR table parser + FRD/attachment matching
tools/cr-agent/prompts/          # plan.md, implement.md (edit these to tune behaviour)
CLAUDE.md.template               # repo conventions for Claude; fill in and save as CLAUDE.md
```

Copy everything into the INSURE 2.0 repo on the **default branch** (scheduled workflows only run from there), and add `CLAUDE.md` to `insure/develop`.

## Setup

### 1. Azure app registration (to read the mailbox)
Ask your M365 admin to:
1. Register an app in Entra ID, create a client secret.
2. Add **Microsoft Graph → Application permission → `Mail.Read`** and grant admin consent.
3. **Restrict it to your mailbox only** with an Application Access Policy (Exchange Online PowerShell), otherwise `Mail.Read` covers every mailbox in the tenant:
   ```powershell
   New-DistributionGroup -Name "cr-agent-mailboxes" -Type Security -Members shubham.kumar@lumiq.ai
   New-ApplicationAccessPolicy -AppId <CLIENT_ID> -PolicyScopeGroupId cr-agent-mailboxes `
     -AccessRight RestrictAccess -Description "CR agent: FRD mailbox only"
   ```

### 2. GitHub secrets and variables
Repo → Settings → Secrets and variables → Actions.

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API key from the LUMIQ Claude Console org |
| `MS_TENANT_ID`, `MS_CLIENT_ID`, `MS_CLIENT_SECRET` | From step 1 |
| `CR_AGENT_GH_TOKEN` (optional) | Fine-grained PAT / GitHub App token with `contents: write`. Needed only if you want your CI to run on the pushed CR branches |

| Variable (optional) | Default |
|---|---|
| `CR_AGENT_MAILBOX` | `shubham.kumar@lumiq.ai` |
| `CR_AGENT_SENDERS` | `dinesh.mehta@canarahsbclife.in` (comma-separated) |
| `CR_AGENT_BASE_BRANCH` | `insure/develop` |
| `CR_AGENT_TEST_COMMAND` | e.g. `mvn -q test && npm --prefix web test -- --watch=false` |
| `CR_AGENT_MODEL` | Claude Code default |

Also edit the `TODO` step in the workflow to install the INSURE toolchain (JDK/Maven, Node, etc.) so Claude can build and run tests.

### 3. Instant trigger from Outlook (optional, Power Automate)
The workflow already polls every 30 minutes. For near-instant pickup:
- Trigger: **When a new email arrives (V3)**, Subject filter `FRDS`, From `dinesh.mehta@canarahsbclife.in`, Include attachments: No.
- Action: **HTTP** (premium connector)
  `POST https://api.github.com/repos/<org>/<repo>/dispatches`
  Headers: `Authorization: Bearer <PAT with contents:write>`, `Accept: application/vnd.github+json`
  Body: `{"event_type": "frd-mail"}`

### 4. First run
Actions → **CR Agent** → Run workflow with **dry_run = true**. The summary shows which CRs would get branches, which are waiting for FRDs, and which are skipped. Then run with `dry_run = false` and `only_cr = CR-18954` to try a single CR before letting it run on everything.

## Behaviour notes
- **Idempotent:** a CR is processed once. To redo one, delete its remote branch and re-run with `only_cr`.
- **Next lot of FRDs:** new emails with a matching subject are picked up automatically. If the subject changes, adjust `SUBJECT_REGEX` (default `insure\s*2\.0.*phase\s*-?\s*2.*frd`).
- **Shared FRDs:** SIRS-117 / SIRS-118 appear both as their own rows and inside CR-45276, so they get their own branches *and* are part of CR-45276's input. Tell the reviewers, or remove them from one side via `only_cr`.
- **Mismatched filenames** (e.g. Odisha/Bihar onboarding FRDs named `CR-45159_...`) are processed but flagged as warnings in the plan and the run summary.
- **Guardrails:** Claude can't commit, push, checkout, reset or rebase (`DISALLOWED_TOOLS`); git is done by the script. FRDs are kept out of the repo (`.cr-agent/` is git-excluded) unless `COMMIT_FRD=true`.
- **Cost/time:** each CR is two Claude Code sessions. Cap them with `PLAN_MAX_TURNS` (40) and `IMPL_MAX_TURNS` (150).

## Before switching it on
- The FRDs are classified **Restricted** by Canara HSBC. Confirm the LUMIQ–Canara agreement allows sending them to the Claude API.
- Treat every branch as a first draft: review against the REQ checklist in `plan.md`, check the `TODO(<CR>)` markers and open questions in `implementation-notes.md`, then raise the PR yourselves.

Docs: [Claude Code headless mode](https://docs.claude.com/en/docs/claude-code/headless)
