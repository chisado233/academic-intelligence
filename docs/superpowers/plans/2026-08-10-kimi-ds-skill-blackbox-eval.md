# Kimi DeepSeek Skill Black-box Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute and independently verify 50 isolated Academic Intelligence tasks with `kimi` + `opencode-go/deepseek-v4-flash` using only the public `SKILL.md` contract.

**Architecture:** A manifest defines immutable task instructions and hidden evaluator expectations. A generator creates one isolated cwd and prompt per task; the local scheduler runs exact-model identity jobs without fallback. The main Agent validates scheduler state, result schema, command evidence, artifacts, and task-specific facts before producing the scorecard.

**Tech Stack:** PowerShell 7, Python 3.12 standard library, `mycli agent-cli`, Academic Intelligence 0.1.0 clean wheel environment.

## Global Constraints

- Exact execution target: `kimi` + `opencode-go/deepseek-v4-flash`; no fallback model.
- Every task has a fresh run, session, cwd, and output directory.
- Worker may read only `SKILL.md` and use only the installed Academic Intelligence Python/CLI for product behavior.
- Worker may write only its task cwd and scheduler-owned archive.
- No project source/tests/README/other docs, direct HTTP/web search, Git, mycli, or sub-agent use by Worker.
- Main Agent owns dispatch, hidden expectations, verification, project docs, and final judgment.
- Do not commit in the existing 188-entry dirty worktree.

---

### Task 1: Freeze the Suite Contract

**Files:**
- Read: `docs/superpowers/specs/2026-08-10-kimi-ds-skill-blackbox-eval-design.md`
- Create: `D:\agent_workspace\tmp\agent-dispatch\20260810-kimi-ds-paper-skill-eval50\suite.json`

**Interfaces:**
- Consumes: the 50 frozen task IDs K01-K50.
- Produces: machine-readable task metadata with `id`, `name`, `network`, `instructions`, `required_observations`, and hidden `oracle`.

- [ ] Create all 50 manifest records with no placeholders.
- [ ] Validate unique IDs, exact count 50, and contiguous K01-K50 ordering.
- [ ] Confirm prompts expose tasks but not hidden oracle values.

### Task 2: Build Isolated Prompt Generation

**Files:**
- Create: `D:\agent_workspace\tmp\agent-dispatch\20260810-kimi-ds-paper-skill-eval50\generate_prompts.py`
- Generate: `...\tasks\K01..K50\prompt.md`

**Interfaces:**
- Consumes: `suite.json`.
- Produces: per-task cwd and a self-contained prompt containing boundaries, absolute CLI/Python paths, task instructions, and the standard `result.json` schema.

- [ ] Write generator using Python standard library only.
- [ ] Generate 50 prompts and task directories.
- [ ] Scan every prompt for exact model-independent black-box constraints and absence of oracle data.

### Task 3: Create the Scheduler Job and Pilot K01

**Files:**
- Write only scheduler-owned archives and `...\job.json`.

**Interfaces:**
- Command: `mycli agent-cli job init --name paper-skill-kimi-ds-eval50 --desc ... --mode medium`.
- Command: `mycli agent-cli run kimi --model opencode-go/deepseek-v4-flash --job <jobId> --name prc-eval50-k01 --task-file <prompt> --cwd <task-dir>`.

- [ ] Create the job and record job ID.
- [ ] Dispatch K01 without a fallback model.
- [ ] Wait for terminal state and inspect scheduler status/logs.
- [ ] Require valid `result.json`, actual command evidence, and correct root-help observations before releasing later batches.

### Task 4: Execute the Local Batches

**Files:**
- Read: K02-K05, K24-K43, K45-K47 prompts.
- Write: corresponding task cwd artifacts and scheduler archives.

**Interfaces:**
- Produces: one run ID and terminal state per task.

- [ ] Dispatch Local-1 at up to 10 concurrent runs.
- [ ] Wait for every Local-1 terminal state; do not infer success from process exit alone.
- [ ] Dispatch Local-2 at up to 10 concurrent runs.
- [ ] Wait for every Local-2 terminal state and preserve logs.

### Task 5: Execute the Live Data Batches

**Files:**
- Read: K06-K23 prompts.
- Write: task-local reports, JSON, databases, snapshots, and source outputs.

**Interfaces:**
- Academic data access must occur only through the installed package.

- [ ] Dispatch K06-K15 with at most 4 concurrent runs.
- [ ] Dispatch K16-K23 with at most 3 concurrent runs.
- [ ] Record HTTP/rate-limit/network failures as evidence; never substitute a model or direct data access.

### Task 6: Execute Integrated and Repeat Tasks

**Files:**
- Read: K44, K48-K50 prompts.
- Write: integrated artifacts and repeat evidence.

**Interfaces:**
- K50 consumes no K06 context; the main evaluator compares their independently produced facts afterward.

- [ ] Run K44, K48, and K49 with at most 2 concurrent runs.
- [ ] Run K50 only after K06 has a terminal result.
- [ ] Compare K50 and K06 title, DOI, year, and core author set.

### Task 7: Independently Evaluate All Results

**Files:**
- Create: `...\evaluate.py`
- Create: `...\evaluation.json`
- Create: `...\evaluation.md`

**Interfaces:**
- Consumes: manifest, scheduler run metadata/logs, 50 task directories.
- Produces: main-Agent verdict per task and aggregate scores.

- [ ] Validate result schema and task ID for each task.
- [ ] Detect forbidden command evidence (`curl`, browser/search, direct requests/httpx data access, Git, mycli, source/test reads).
- [ ] Open and verify SQLite/JSON/snapshot/CSV/JSONL artifacts for deterministic tasks.
- [ ] Check known paper/author facts and evidence sources for live tasks.
- [ ] Classify `PASS`, `PARTIAL`, `FAIL`, or `BLOCKED` with a concrete reason and evidence path.
- [ ] Calculate completion rate, capability pass rate, blocked count, and core-gate status.

### Task 8: Close the Evaluation

**Files:**
- Modify: `docs/progress.md`
- Modify: `docs/decisions.md` only if the evaluation changes a project-level decision.
- Write: `...\run-index.json` and final scorecard.

**Interfaces:**
- Consumes: final evaluation and job status.
- Produces: durable project progress plus a user-facing result.

- [ ] Retry only evidence-format mistakes in the original session; do not retry factual failures until they disappear.
- [ ] Close the scheduler job after every run reaches a terminal state.
- [ ] Update progress with exact commands, counts, outcomes, blockers, and uncovered risks.
- [ ] Run `mkdocs build --strict` after project documentation changes.
- [ ] Report model identity, task matrix, evidence locations, failures, limitations, and Git state.

## Plan Self-review

- Spec coverage: all K01-K50 map to an execution batch and independent evaluation.
- Placeholder scan: no TBD/TODO/future implementation placeholders.
- Interface consistency: exact model, paths, task IDs, output schema, and verdict names match the design.
- Scope: evaluation only; no source changes or product fixes are authorized in this plan.
