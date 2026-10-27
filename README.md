# INSURE CR Agent: FRD email → CR branches in backend + frontend

This repo (`insure-cr-agent`) holds the agent, the workflow and the `frd-inbox` branch.
The product repos (backend, frontend) only receive `CR-xxxxx` branches.

```
Dinesh sends FRD mail
  → Power Automate saves body + attachments into frd-inbox/<timestamp>/ of THIS repo, then pings GitHub
  → GitHub Actions clones backend/ and frontend/ side by side into one workspace
  → per CR: CR-xxxxx cut from insure/develop in each repo
       Claude Code pass 1: plan.md with requirement checklist + API contract (sees both repos)
       Claude Code pass 2: code + tests in whichever repos need it, in ONE session
  → each changed repo: commit plan, commit code, push CR-xxxxx
```

## Setup checklist
1. Create private repo `insure-cr-agent`; copy this kit in (default branch `main`).
2. Create the orphan `frd-inbox` branch in it.
3. Add a `CLAUDE.md` (from `CLAUDE.md.template`) to `insure/develop` of **each** product repo.
4. Fine-grained GitHub token with access to backend, frontend and insure-cr-agent, Contents: Read and write.
5. Secrets in insure-cr-agent: `CLAUDE_CODE_OAUTH_TOKEN` (Team seat, from `claude setup-token`) or `ANTHROPIC_API_KEY`; `CR_AGENT_GH_TOKEN` (step 4).
6. Variables: `CR_AGENT_REPOS` = `backend=<org>/<backend-repo>@insure/develop,frontend=<org>/<frontend-repo>@insure/develop`,
   `CR_AGENT_TEST_BACKEND`, `CR_AGENT_TEST_FRONTEND`, `CR_AGENT_DRY_RUN`, `CR_AGENT_SENDERS`.
7. Adjust toolchain versions in `.github/workflows/cr-agent.yml` (Java, Node, registries).
8. Power Automate flow pointing at `<org>/insure-cr-agent` (same flow as before).
9. Dry run → one CR → all → go live.

## Behaviour
- Each plan starts with `SCOPE: frontend`, `SCOPE: backend` or `SCOPE: backend, frontend`. UI-only CRs (labels, layout, client-side validations, journey/lookup config, translations) stay frontend-only; edits Claude makes outside the scope are discarded and reported as a warning. The scope appears in the run summary.
- Claude reads the mails as well as the FRDs: every mail in the batch (oldest first, duplicates dropped, inline images included) is given to it as requirement context. A newer mail overrides the FRD where they disagree, and each REQ in plan.md names its source (FRD section or Mail n).
- A CR whose FRD column says e.g. "Refer mail below", or a CR id written only in the mail text, is built from the mail alone (the latter is flagged for review). Turn the text-only case off with `MAIL_ONLY_CRS=false`.
- Mail text is treated as data, not instructions; Claude runs without the GitHub token in its environment, and the token is never written to `.git/config`.
- One Claude session per CR sees both repos, so the API contract, field names and validations match.
- A CR's branch is pushed only to repos with changes; commits name the companion repo.
- A CR is processed once (skipped if `CR-xxxxx` exists in any product repo). To redo: delete the branch in all repos and run with `only_cr`.
- NA / "No changes in INSURE2.0" CRs are skipped; CRs whose FRD arrives later wait for it; filename mismatches are flagged.
- Claude cannot commit/push/checkout/reset; the script does git. Branches are never merged automatically.
- Merge order for cross-repo CRs: usually backend first (see implementation-notes.md).

| File | Purpose |
|---|---|
| `.github/workflows/cr-agent.yml` | Power Automate ping, hourly safety net, manual runs |
| `tools/cr-agent/main.py` | Multi-repo orchestration and git |
| `tools/cr-agent/gitinbox.py` | Reads FRD mails from `frd-inbox` |
| `tools/cr-agent/graph.py` | Alternative: read Outlook directly (`MAIL_SOURCE=graph`) |
| `tools/cr-agent/parse.py` | CR table parser + FRD matching |
| `tools/cr-agent/prompts/` | Plan and implement prompts |
| `CLAUDE.md.template` | Per-repo conventions for Claude |
