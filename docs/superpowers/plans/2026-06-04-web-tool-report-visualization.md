# Web Tool Report Visualization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn Web auxiliary tool runs from "live stdout only" into structured visual reports for manual probes and model enumeration.

**Architecture:** Keep the current subprocess-based Web runner. Add JSON artifacts for `manual_probe.py`, reuse `model_enum.py` JSON output, then teach `web.server` to expose tool reports and `web/static/app.js` to render them.

**Tech Stack:** Python stdlib HTTP server, subprocess runner, SSE logs, plain HTML/CSS/JS, unittest.

---

## Scope

Phase 1 already lets Web run:

- Full audit
- `manual_probe.py`
- `model_enum.py`

Phase 2 adds structured visualization only. It must not require real API calls for automated tests.

## Files

- Modify: `manual_probe.py`
  - Add `--json-out`.
  - Persist step return values as JSON.
- Modify: `web/server.py`
  - Pass `--json-out` for manual probe Web runs.
  - Register `manual-probe.json` artifact.
  - Add tool report loading for `manual_probe` and `model_enum`.
- Modify: `web/static/app.js`
  - Render model enum rows as a table.
  - Render manual probe step results as cards.
- Modify: `web/static/index.html`
  - Add containers if current report panel is insufficient.
- Modify: `web/static/styles.css`
  - Add compact table/status styles for tool reports.
- Modify: `README.md`
  - Document structured Web tool reports.
- Modify: `CHANGELOG.md`
  - Add Phase 2 entry.
- Test: `tests/test_aux_scripts.py`
  - Verify `manual_probe.py --json-out` writes expected JSON without network.
- Test: `tests/test_web_server.py`
  - Verify tool report artifact paths and report loading.

## Tasks

### Task 1: Manual Probe JSON Output

- [ ] Add `json_out: Path | None` to `manual_probe.Config`.
- [ ] Add `--json-out` CLI option.
- [ ] Change `main()` to collect each step return value into:

```json
{
  "tool": "manual_probe",
  "base": "https://relay.example.com",
  "model": "gemini-3.1-pro-preview",
  "sig_model": "gemini-3-flash-preview",
  "steps": [
    {"step": "1a", "status": 200},
    {"step": "2a", "signal": "oauth_accepted_as_is"}
  ]
}
```

- [ ] If `--json-out` is set, create parent directories and write UTF-8 JSON.
- [ ] Unit test with mocked step functions or mocked HTTP call; no network.

### Task 2: Web Tool Artifacts

- [ ] For `manual_probe` Web runs, set output path:

```text
reports/manual-probe-<runid>.json
```

- [ ] Add `manual-probe.json` to `RunState.artifacts`.
- [ ] Keep `model-enum.json` behavior:

```text
reports/model-enum-<runid>.json
```

- [ ] Add a helper:

```python
def load_tool_report(run: RunState) -> dict[str, Any]:
    ...
```

- [ ] Expose it from:

```text
GET /api/runs/<id>/tool-report
```

- [ ] Unit test success and missing artifact cases.

### Task 3: Model Enum Visualization

- [ ] In `app.js`, after `model_enum` completes, fetch `/api/runs/<id>/tool-report`.
- [ ] Render a table with columns:
  - model
  - status
  - modelVersion
  - alias
  - nothinking
  - trafficType
  - serviceTier
  - error
- [ ] Highlight:
  - OK rows
  - failed rows
  - alias rows
  - nothinking rows
- [ ] Keep raw JSON download link.
- [ ] `node --check web/static/app.js`.

### Task 4: Manual Probe Visualization

- [ ] Fetch `/api/runs/<id>/tool-report` after manual probe completion.
- [ ] Render step cards:
  - `1a`: headers + modelVersion + usageMetadata fields
  - `1b`: error leak message and interesting headers
  - `1c`: countTokens fields and endpoint pollution
  - `2a`: `signal`, status, modelVersion, elapsed
  - `2b`: identity text and keyword flags
  - `3a`: knowledge verdict and per-question outcomes
  - `3b`: self sig verdict and pass/fail counters
- [ ] Keep stdout terminal as audit trail.
- [ ] `node --check web/static/app.js`.

### Task 5: Documentation

- [ ] Update README Web section:
  - Manual probe Web run now produces a structured report.
  - Model enum Web run renders a table and downloads JSON.
- [ ] Update CHANGELOG `[Unreleased]` Web section.

### Task 6: Verification

- [ ] Run:

```bash
python -m unittest tests.test_web_server tests.test_aux_scripts
```

- [ ] Run:

```bash
python -m py_compile audit.py manual_probe.py model_enum.py probes\__init__.py probes\active.py probes\tier4.py probes\verdict.py probes\report.py web\server.py
```

- [ ] Run:

```bash
node --check web\static\app.js
```

- [ ] Run:

```bash
git diff --check
```

- [ ] Scan for real credential remnants:

```bash
rg -n "REAL_KEY_FRAGMENT|REAL_RELAY_IP" manual_probe.py model_enum.py README.md CHANGELOG.md web tests
```

- [ ] Optional local Web smoke:
  - Start `python -m web.server --host 127.0.0.1 --port 18081`.
  - Confirm `/` returns 200.
  - Confirm missing-key validation on both tool endpoints.
  - Stop the temporary process.

## Out Of Scope

- Running real external relay audits in automated verification.
- Replacing subprocess execution with direct Python function calls.
- Persisting run history across Web server restarts.
- Multi-user auth beyond existing `WEB_AUTH_TOKEN`.
