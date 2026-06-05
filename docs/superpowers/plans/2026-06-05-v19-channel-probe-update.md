# V1.9 Channel Probe Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Update the relay detection scripts so their automated verdicts match `Gemini 中转站渠道识别方案 v1.9`, especially the model-aware `thinkingBudget=0` rule.

**Architecture:** Add one shared thinking-budget classifier under `probes/`, then reuse it from `manual_probe.py`, `probes/active.py`, `probes/verdict.py`, and `model_source_probe.py`. Keep raw HTTP capture unchanged, but enrich summaries and reports with model-aware verdict fields so Flash `thinkingBudget=0 -> 200` is not misclassified as OAuth.

**Tech Stack:** Python stdlib (`dataclasses`, `typing`, `json`, `urllib`), existing `unittest` test suite, existing script artifacts under `reports/`.

---

## File Structure

- Create: `probes/thinking.py`
  - Owns Gemini model capability classification for `thinkingBudget=0`.
  - Exposes a small pure function used by CLI scripts and verdict scoring.

- Modify: `manual_probe.py`
  - Replace local `classify_thinking_budget_result()` logic with `probes.thinking`.
  - Pass requested model into the classifier.

- Modify: `probes/active.py`
  - Replace v1.8 docstring and hardcoded OAuth interpretation.
  - Store structured v1.9 fields from the shared classifier in `p1-thinkingBudget0.json` and `verdict.json`.

- Modify: `probes/verdict.py`
  - Stop treating all `thinkingBudget=0 -> 200` results as OAuth hard evidence.
  - Only count unexpected accepts for Pro / no-thinking-disallowed routes, and require cross evidence for final OAuth where appropriate.

- Modify: `probes/report.py`
  - Default scheme version to `v1.9`.
  - Render the model-aware thinking verdict and caveat.

- Modify: `audit.py`
  - Bump `tool_version` and `scheme_version`.

- Modify: `model_source_probe.py`
  - Add v1.9 model-aware classification for `thinking-zero` records.
  - Add countTokens endpoint pollution summary.
  - Keep full `.http` transcripts unchanged.

- Modify: `web/static/app.js`
  - Rename the display label to make clear `thinkingBudget=0` is model-aware.
  - Surface `expected_behavior` / `oauth_suspect` when present in tool reports.

- Modify: `tests/test_aux_scripts.py`
  - Replace old `oauth_accepted_as_is` expectation for generic 200.
  - Add explicit cases for Flash allowed 200 and Pro unexpected 200.

- Create: `tests/test_thinking_v19.py`
  - Unit tests for shared thinking classifier.

- Modify: `tests/test_web_server.py` only if the Web label or command output expectations change.

- Modify docs:
  - `CHANGELOG.md`
  - `COVERAGE.md`
  - `README.md` if it describes `thinkingBudget=0` semantics.

---

### Task 1: Add Shared V1.9 Thinking Classifier

**Files:**
- Create: `probes/thinking.py`
- Create: `tests/test_thinking_v19.py`

- [ ] **Step 1: Write failing tests for model-aware classification**

Create `tests/test_thinking_v19.py` with:

```python
import unittest

from probes.thinking import classify_thinking_budget_zero, model_zero_capability


class ThinkingBudgetV19Test(unittest.TestCase):
    def test_flash_zero_200_is_allowed_not_oauth(self):
        result = classify_thinking_budget_zero(
            requested_model="gemini-2.5-flash",
            status=200,
            body={"modelVersion": "gemini-2.5-flash"},
            elapsed=1.2,
        )

        self.assertEqual(result["capability"], "supports_zero")
        self.assertEqual(result["signal"], "zero_supported_accept")
        self.assertFalse(result["oauth_suspect"])
        self.assertFalse(result["hard_oauth_evidence"])

    def test_flash_lite_zero_200_is_allowed(self):
        result = classify_thinking_budget_zero(
            requested_model="gemini-2.5-flash-lite",
            status=200,
            body={"modelVersion": "gemini-2.5-flash-lite"},
            elapsed=0.9,
        )

        self.assertEqual(result["capability"], "supports_zero")
        self.assertEqual(result["signal"], "zero_supported_accept")

    def test_pro_zero_400_is_expected_strict_reject(self):
        result = classify_thinking_budget_zero(
            requested_model="gemini-2.5-pro",
            status=400,
            body={"error": {"message": "Budget 0 is invalid. This model only works in thinking mode."}},
            elapsed=0.4,
        )

        self.assertEqual(result["capability"], "requires_thinking")
        self.assertEqual(result["signal"], "strict_reject_expected")
        self.assertFalse(result["oauth_suspect"])

    def test_pro_zero_200_is_unexpected_accept(self):
        result = classify_thinking_budget_zero(
            requested_model="gemini-2.5-pro",
            status=200,
            body={"modelVersion": "gemini-2.5-pro"},
            elapsed=19.0,
        )

        self.assertEqual(result["capability"], "requires_thinking")
        self.assertEqual(result["signal"], "unexpected_accept")
        self.assertTrue(result["oauth_suspect"])
        self.assertTrue(result["latency_warning"])

    def test_nothinking_alias_is_rewrite_not_hard_oauth(self):
        result = classify_thinking_budget_zero(
            requested_model="gemini-2.5-flash",
            status=200,
            body={"modelVersion": "gemini-2.5-flash-nothinking"},
            elapsed=1.5,
        )

        self.assertEqual(result["signal"], "rewritten_to_nothinking")
        self.assertFalse(result["hard_oauth_evidence"])

    def test_model_capability_rules(self):
        self.assertEqual(model_zero_capability("gemini-2.5-flash"), "supports_zero")
        self.assertEqual(model_zero_capability("gemini-2.5-flash-lite"), "supports_zero")
        self.assertEqual(model_zero_capability("gemini-2.5-pro"), "requires_thinking")
        self.assertEqual(model_zero_capability("gemini-3-pro-image-preview"), "requires_thinking")
        self.assertEqual(model_zero_capability("gemini-3.5-flash"), "flash_compat")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the new test and verify it fails**

Run:

```powershell
python -m unittest tests.test_thinking_v19
```

Expected: import failure because `probes.thinking` does not exist.

- [ ] **Step 3: Implement the shared classifier**

Create `probes/thinking.py`:

```python
from __future__ import annotations

from typing import Any


SCHEME_VERSION = "v1.9"


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def model_zero_capability(model: str, model_version: str | None = None) -> str:
    """Return how `thinkingBudget=0` should be interpreted for this model.

    supports_zero:
        Officially supported no-thinking behavior, currently 2.5 Flash and
        2.5 Flash-Lite families. A 200 here is not OAuth evidence.

    requires_thinking:
        Pro routes and Pro image-preview routes should reject zero; a 200 here
        is an unexpected accept that needs OAuth / rewrite cross evidence.

    flash_compat:
        Newer Flash-family model where 0 may be accepted for compatibility or
        ignored. A 200 here is only an auxiliary observation.

    unknown:
        No hard conclusion from status alone.
    """
    combined = f"{_norm(model)} {_norm(model_version)}"
    if "nothinking" in combined:
        return "rewritten_alias"
    if "flash-lite" in combined and "2.5" in combined:
        return "supports_zero"
    if "flash" in combined and "2.5" in combined and "pro" not in combined:
        return "supports_zero"
    if "pro" in combined:
        return "requires_thinking"
    if "flash" in combined:
        return "flash_compat"
    return "unknown"


def classify_thinking_budget_zero(
    *,
    requested_model: str,
    status: int,
    body: dict[str, Any],
    elapsed: float,
) -> dict[str, Any]:
    model_version = str(body.get("modelVersion") or "")
    capability = model_zero_capability(requested_model, model_version)
    error_message = ""
    error = body.get("error")
    if isinstance(error, dict):
        error_message = str(error.get("message") or "")

    latency_warning = elapsed >= 15.0 or status == -1

    if status == 400:
        if capability == "requires_thinking":
            signal = "strict_reject_expected"
            note = "Pro / requires-thinking model rejected thinkingBudget=0 as expected."
        else:
            signal = "strict_reject"
            note = "The upstream rejected thinkingBudget=0; this is strict but not OAuth evidence."
        oauth_suspect = False
        hard_oauth_evidence = False
    elif status == 200 and "nothinking" in model_version.lower():
        signal = "rewritten_to_nothinking"
        note = "Relay or upstream returned a -nothinking alias; treat as rewrite evidence, not standalone OAuth proof."
        oauth_suspect = capability == "requires_thinking"
        hard_oauth_evidence = False
    elif status == 200 and capability == "supports_zero":
        signal = "zero_supported_accept"
        note = "Flash / Flash-Lite accepts thinkingBudget=0 by official behavior; not OAuth evidence."
        oauth_suspect = False
        hard_oauth_evidence = False
    elif status == 200 and capability == "flash_compat":
        signal = "flash_compat_accept"
        note = "Flash-family compatibility accept; record latency and fields but do not classify as OAuth."
        oauth_suspect = False
        hard_oauth_evidence = False
    elif status == 200 and capability == "requires_thinking":
        signal = "unexpected_accept"
        note = "Requires-thinking model accepted thinkingBudget=0; OAuth or relay rewrite is suspected and needs cross evidence."
        oauth_suspect = True
        hard_oauth_evidence = True
    elif status == -1:
        signal = "timeout_or_network"
        note = "Timeout or network failure; high latency is only auxiliary evidence."
        oauth_suspect = False
        hard_oauth_evidence = False
    else:
        signal = f"status_{status}"
        note = "No standalone conclusion from this status."
        oauth_suspect = False
        hard_oauth_evidence = False

    return {
        "classification_version": SCHEME_VERSION,
        "requested_model": requested_model,
        "modelVersion": model_version,
        "capability": capability,
        "status": status,
        "elapsed_seconds": round(float(elapsed), 3),
        "signal": signal,
        "note": note,
        "error_message": error_message,
        "latency_warning": latency_warning,
        "oauth_suspect": oauth_suspect,
        "hard_oauth_evidence": hard_oauth_evidence,
    }
```

- [ ] **Step 4: Run the new test and verify it passes**

Run:

```powershell
python -m unittest tests.test_thinking_v19
```

Expected: `OK`.

---

### Task 2: Update Manual Probe CLI

**Files:**
- Modify: `manual_probe.py`
- Modify: `tests/test_aux_scripts.py`

- [ ] **Step 1: Replace the old test expectations**

In `tests/test_aux_scripts.py`, replace `test_thinking_budget_result_flags_oauth_and_nothinking` with:

```python
    def test_thinking_budget_result_is_model_aware(self):
        import manual_probe

        flash_ok = manual_probe.classify_thinking_budget_result(
            "gemini-2.5-flash",
            200,
            {"modelVersion": "gemini-2.5-flash"},
            elapsed=1.5,
        )
        pro_bad = manual_probe.classify_thinking_budget_result(
            "gemini-2.5-pro",
            200,
            {"modelVersion": "gemini-2.5-pro"},
            elapsed=19.0,
        )
        rewritten = manual_probe.classify_thinking_budget_result(
            "gemini-2.5-flash",
            200,
            {"modelVersion": "gemini-2.5-flash-nothinking"},
            elapsed=1.5,
        )
        strict = manual_probe.classify_thinking_budget_result(
            "gemini-2.5-pro",
            400,
            {"error": {"message": "invalid thinkingBudget"}},
            elapsed=0.4,
        )

        self.assertEqual(flash_ok["signal"], "zero_supported_accept")
        self.assertFalse(flash_ok["oauth_suspect"])
        self.assertEqual(pro_bad["signal"], "unexpected_accept")
        self.assertTrue(pro_bad["oauth_suspect"])
        self.assertEqual(rewritten["signal"], "rewritten_to_nothinking")
        self.assertEqual(strict["signal"], "strict_reject_expected")
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```powershell
python -m unittest tests.test_aux_scripts.ManualProbeTest.test_thinking_budget_result_is_model_aware
```

Expected: failure because `classify_thinking_budget_result()` still accepts only `(status, data, elapsed)`.

- [ ] **Step 3: Update `manual_probe.py` to delegate classification**

Change imports:

```python
from probes.thinking import classify_thinking_budget_zero
```

Replace `classify_thinking_budget_result` with:

```python
def classify_thinking_budget_result(
    model: str,
    status: int,
    data: dict[str, Any],
    elapsed: float,
) -> dict[str, Any]:
    return classify_thinking_budget_zero(
        requested_model=model,
        status=status,
        body=data,
        elapsed=elapsed,
    )
```

In `step2_thinking_budget_zero()`, change:

```python
result = classify_thinking_budget_result(status, data, elapsed)
```

to:

```python
result = classify_thinking_budget_result(cfg.model, status, data, elapsed)
```

Add these two output lines after the signal line:

```python
print(f"    capability: {result.get('capability')}")
print(f"    oauth_suspect: {result.get('oauth_suspect')}")
```

- [ ] **Step 4: Run targeted tests**

Run:

```powershell
python -m unittest tests.test_aux_scripts.ManualProbeTest
```

Expected: `OK`.

---

### Task 3: Update Active Audit Probe Output

**Files:**
- Modify: `probes/active.py`

- [ ] **Step 1: Update `probe_thinking_budget_zero()` docstring and imports**

Add:

```python
from .thinking import classify_thinking_budget_zero
```

Replace the old docstring with:

```python
    """Probe thinkingBudget=0 using scheme v1.9 model-aware semantics.

    Flash / Flash-Lite accepting 0 is official behavior and not OAuth proof.
    Pro / no-thinking-disallowed routes accepting 0 are OAuth or relay-rewrite
    suspects and must be cross-checked with routing headers, identity, and sig.
    """
```

- [ ] **Step 2: Return structured classifier fields**

After the request and `save_raw(...)`, add:

```python
    verdict = classify_thinking_budget_zero(
        requested_model=model,
        status=status,
        body=data,
        elapsed=0.0,
    )
```

Then ensure the returned dict includes at least:

```python
        "classification_version": verdict["classification_version"],
        "capability": verdict["capability"],
        "signal": verdict["signal"],
        "note": verdict["note"],
        "oauth_suspect": verdict["oauth_suspect"],
        "hard_oauth_evidence": verdict["hard_oauth_evidence"],
        "latency_warning": verdict["latency_warning"],
```

If `post_generate()` does not expose elapsed time, leave `elapsed=0.0` for this task and add a later improvement to return elapsed from `post_generate()`; do not guess.

- [ ] **Step 3: Add a focused unit test if `probe_thinking_budget_zero()` has testable pure logic**

If no pure logic remains in `active.py`, skip adding a new active-specific test and rely on `tests/test_thinking_v19.py`. Do not mock network calls just for this task.

- [ ] **Step 4: Run py_compile**

Run:

```powershell
python -m py_compile probes\active.py probes\thinking.py
```

Expected: no output and exit code `0`.

---

### Task 4: Update Verdict Scoring

**Files:**
- Modify: `probes/verdict.py`
- Create or modify: `tests/test_verdict_v19.py`

- [ ] **Step 1: Write failing verdict tests**

Create `tests/test_verdict_v19.py`:

```python
import unittest

from probes import verdict


def raw_for(active: dict) -> dict:
    return {
        "meta": {"keys": ["k1"]},
        "per_key": {"k1": {"active": active, "tier4": {}}},
        "cross_sig_matrix": None,
    }


class VerdictV19Test(unittest.TestCase):
    def test_flash_thinking_zero_200_does_not_create_oauth_verdict(self):
        raw = raw_for({
            "thinkingBudget_zero": {
                "status": 200,
                "signal": "zero_supported_accept",
                "capability": "supports_zero",
                "oauth_suspect": False,
                "hard_oauth_evidence": False,
            },
            "field_sampling": {
                "n": 5,
                "valid_n": 5,
                "upstream_count": {"aistudio_likely": 5},
                "modelVersion_count": {"gemini-2.5-flash": 5},
                "placeholder_rate": 0.0,
            },
        })

        result = verdict.compute(raw)["per_key"]["k1"]

        self.assertNotEqual(result["label"], "oauth_wrapper")

    def test_pro_unexpected_accept_with_routing_group_is_oauth(self):
        raw = raw_for({
            "thinkingBudget_zero": {
                "status": 200,
                "signal": "unexpected_accept",
                "capability": "requires_thinking",
                "oauth_suspect": True,
                "hard_oauth_evidence": True,
            },
            "http_headers": {
                "headers": {"x-routing-group": "gemini-cli"},
            },
        })

        result = verdict.compute(raw)["per_key"]["k1"]

        self.assertEqual(result["label"], "oauth_wrapper")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
python -m unittest tests.test_verdict_v19
```

Expected: failure because v1.8 logic still counts raw `thinking_accepted` as OAuth hard evidence.

- [ ] **Step 3: Update evidence creation for `thinkingBudget_zero`**

In `_judge_one()`, replace the current status-only thinking logic with fields from the classifier:

```python
    tb0 = active.get("thinkingBudget_zero") or {}
    if tb0:
        signal = tb0.get("signal")
        capability = tb0.get("capability")
        fact = f"{tb0.get('status')} {signal or ''}".strip()
        implication = tb0.get("note") or "thinkingBudget=0 probe recorded."
        evidence.append({
            "tier": 2,
            "name": "thinkingBudget=0",
            "fact": fact,
            "implication": implication,
        })
        if tb0.get("hard_oauth_evidence"):
            flags["thinking_unexpected_accept"] = True
        if tb0.get("oauth_suspect"):
            flags["thinking_oauth_suspect"] = True
        if tb0.get("latency_warning"):
            flags["thinking_timeout"] = True
        if signal in {"strict_reject_expected", "strict_reject"}:
            flags["strict_400"] = True
        if signal == "rewritten_to_nothinking":
            flags["thinking_rewritten"] = True
        if capability in {"supports_zero", "flash_compat"} and tb0.get("status") == 200:
            flags["thinking_zero_allowed"] = True
```

Remove or stop setting `flags["thinking_accepted"]`.

- [ ] **Step 4: Update `_decide()` OAuth scoring**

Replace:

```python
    if flags.get("thinking_accepted") and not flags.get("strict_400"):
        oauth_signals += 2
```

with:

```python
    if flags.get("thinking_unexpected_accept"):
        oauth_signals += 1
```

Keep latency and rewrite as auxiliary only:

```python
    if flags.get("thinking_rewritten") and oauth_signals >= 1:
        oauth_signals += 1
    if flags.get("thinking_timeout") and oauth_signals >= 1:
        oauth_signals += 1
```

Update the `_decide()` docstring from `v1.8` to `v1.9`.

- [ ] **Step 5: Run verdict tests**

Run:

```powershell
python -m unittest tests.test_verdict_v19
```

Expected: `OK`.

---

### Task 5: Update Reports and Metadata

**Files:**
- Modify: `audit.py`
- Modify: `probes/report.py`

- [ ] **Step 1: Bump metadata**

In `audit.py`, change:

```python
"tool_version": "1.1.4",
"scheme_version": "v1.8",
```

to:

```python
"tool_version": "1.1.5",
"scheme_version": "v1.9",
```

In `probes/report.py`, change the default:

```python
meta.get('scheme_version', 'v1.8')
```

to:

```python
meta.get('scheme_version', 'v1.9')
```

- [ ] **Step 2: Render v1.9 thinking fields**

In the `#### B. thinkingBudget=0 探针` section, add:

```python
            if tb.get("capability"):
                lines.append(f"- 模型能力判读:`{tb['capability']}`")
            if tb.get("signal"):
                lines.append(f"- v1.9 信号:`{tb['signal']}`")
            if "oauth_suspect" in tb:
                lines.append(f"- OAuth 嫌疑:`{tb.get('oauth_suspect')}`")
            if tb.get("note"):
                lines.append(f"- 说明:{tb['note']}")
```

- [ ] **Step 3: Compile**

Run:

```powershell
python -m py_compile audit.py probes\report.py
```

Expected: no output and exit code `0`.

---

### Task 6: Update `model_source_probe.py` Deep Source Probe

**Files:**
- Modify: `model_source_probe.py`
- Create or modify: `tests/test_model_source_probe_v19.py`

- [ ] **Step 1: Add unit tests for extra probe summarization**

Create `tests/test_model_source_probe_v19.py`:

```python
import unittest

import model_source_probe


class ModelSourceProbeV19Test(unittest.TestCase):
    def test_count_tokens_pollution_detected(self):
        result = model_source_probe.classify_count_tokens_response(
            200,
            {
                "candidates": [{"content": {"parts": [{"text": "hello"}]}}],
                "modelVersion": "gemini-3.5-flash",
            },
        )

        self.assertEqual(result["verdict"], "endpoint_polluted")

    def test_thinking_zero_flash_is_not_oauth(self):
        result = model_source_probe.classify_thinking_zero_record(
            "gemini-3.5-flash",
            {
                "response": {
                    "status": 200,
                    "elapsed_seconds": 73.873,
                    "body": '{"modelVersion":"gemini-3.5-flash"}',
                    "headers": {},
                }
            },
        )

        self.assertIn(result["signal"], {"flash_compat_accept", "zero_supported_accept"})
        self.assertFalse(result["hard_oauth_evidence"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
python -m unittest tests.test_model_source_probe_v19
```

Expected: failure because helper functions do not exist.

- [ ] **Step 3: Add imports and helper functions**

At top of `model_source_probe.py`, add:

```python
from probes.thinking import classify_thinking_budget_zero
```

Add:

```python
def classify_count_tokens_response(status: int, data: dict[str, Any]) -> dict[str, Any]:
    if status != 200:
        return {"verdict": "unavailable", "status": status}
    if "candidates" in data or "modelVersion" in data:
        return {
            "verdict": "endpoint_polluted",
            "note": "countTokens returned generateContent-like fields.",
        }
    if "totalBillableCharacters" in data:
        return {"verdict": "vertex_likely", "note": "countTokens contains totalBillableCharacters."}
    if "totalTokens" in data:
        return {"verdict": "count_tokens_ok", "note": "countTokens schema is present."}
    return {"verdict": "unknown", "note": "Unrecognized countTokens schema."}


def classify_thinking_zero_record(model: str, record: dict[str, Any]) -> dict[str, Any]:
    response = record.get("response", {})
    data = parse_json(response.get("body", ""))
    return classify_thinking_budget_zero(
        requested_model=model,
        status=int(response.get("status", 0)),
        body=data,
        elapsed=float(response.get("elapsed_seconds") or 0.0),
    )
```

- [ ] **Step 4: Store extra probe summaries per model**

In the per-model loop, keep sample records as-is, but track `count_record` and `thinking_record`:

```python
        extra_summary: dict[str, Any] = {}
```

After capturing `count-tokens`, add:

```python
            extra_summary["count_tokens"] = {
                "status": count_record["response"]["status"],
                **classify_count_tokens_response(
                    count_record["response"]["status"],
                    parse_json(count_record["response"]["body"]),
                ),
            }
```

After capturing `thinking-zero`, add:

```python
            extra_summary["thinking_zero"] = classify_thinking_zero_record(model, thinking_record)
```

Then after:

```python
        model_summaries[model] = summarize_records([r for r in records if r["id"].startswith("sample-")])
```

add:

```python
        if extra_summary:
            model_summaries[model]["extra_probes"] = extra_summary
```

Use local variable names `count_record` and `thinking_record` instead of appending anonymous captures directly.

- [ ] **Step 5: Render new columns**

Change the report table header from:

```python
| Model | Status | trafficType | serviceTier | modelVersion | Key Headers |
```

to:

```python
| Model | Status | trafficType | serviceTier | modelVersion | thinkingBudget=0 | countTokens | Key Headers |
```

For each row, compute:

```python
        extras = summary.get("extra_probes", {})
        thinking = extras.get("thinking_zero", {})
        count_tokens = extras.get("count_tokens", {})
        thinking_cell = thinking.get("signal") or "n/a"
        count_cell = count_tokens.get("verdict") or "n/a"
```

Include those two cells before key headers.

- [ ] **Step 6: Add metadata**

In `summary["meta"]`, add:

```python
"scheme_version": "v1.9",
"tool_version": "model-source-probe-v1.1",
```

In `render_report()`, add a line:

```python
f"- Scheme: `{meta.get('scheme_version', 'v1.9')}`",
```

- [ ] **Step 7: Run tests**

Run:

```powershell
python -m unittest tests.test_model_source_probe_v19
```

Expected: `OK`.

---

### Task 7: Update Web Display Labels

**Files:**
- Modify: `web/static/app.js`
- Modify: `web/static/index.html` if visible labels need to match.
- Modify: `tests/test_web_server.py` only if CLI stage label tests break.

- [ ] **Step 1: Update labels only**

In `web/static/app.js`, change:

```javascript
["thinking_budget_zero", "thinkingBudget=0"],
```

to:

```javascript
["thinking_budget_zero", "thinkingBudget=0 (model-aware)"],
```

In `web/static/index.html`, change visible text:

```html
<span>2a thinkingBudget=0</span>
```

to:

```html
<span>2a thinkingBudget=0 (model-aware)</span>
```

- [ ] **Step 2: Run Web tests**

Run:

```powershell
python -m unittest tests.test_web_server
```

Expected: `OK`.

---

### Task 8: Update Documentation

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `COVERAGE.md`
- Modify: `README.md` if it mentions `thinkingBudget=0`.

- [ ] **Step 1: Add changelog entry**

Add near the top of `CHANGELOG.md`:

```markdown
## 1.1.5

- Align detection scripts with `Gemini 中转站渠道识别方案 v1.9`.
- `thinkingBudget=0` is now model-aware:
  - 2.5 Flash / Flash-Lite accepting 0 is normal official behavior.
  - Pro / no-thinking-disallowed routes accepting 0 are OAuth or relay-rewrite suspects only with cross evidence.
- Source probe reports now summarize `thinking-zero` and `countTokens` endpoint pollution while preserving raw `.http` transcripts.
```

- [ ] **Step 2: Fix outdated coverage claims**

In `COVERAGE.md`, replace any line claiming `thinkingBudget=0 返回 200` is a hard OAuth indicator with:

```markdown
`thinkingBudget=0` is model-aware under v1.9. Flash 200 is expected; Pro / disallowed routes returning 200 are OAuth or relay-rewrite suspects requiring cross evidence.
```

- [ ] **Step 3: Search for stale statements**

Run:

```powershell
rg -n "Vertex 一定|Vertex/AI Studio.*拒绝|thinkingBudget=0 返回 200|接受 = OAuth|oauth_accepted_as_is" .
```

Expected: no stale user-facing claims. Test names or historical changelog lines are acceptable only if clearly marked historical and superseded.

---

### Task 9: Full Verification

**Files:**
- No edits unless verification reveals failures.

- [ ] **Step 1: Compile all changed Python files**

Run:

```powershell
python -m py_compile audit.py manual_probe.py model_source_probe.py probes\active.py probes\verdict.py probes\report.py probes\thinking.py
```

Expected: no output and exit code `0`.

- [ ] **Step 2: Run unit tests**

Run:

```powershell
python -m unittest tests.test_aux_scripts tests.test_web_server tests.test_thinking_v19 tests.test_verdict_v19 tests.test_model_source_probe_v19
```

Expected: `OK`.

- [ ] **Step 3: Check CLI help still works**

Run:

```powershell
python manual_probe.py --help
python model_source_probe.py --help
python audit.py --help
```

Expected: all print usage and exit `0`.

- [ ] **Step 4: Optional low-cost dry validation against no network**

Do not call live endpoints in automated tests. Network checks remain manual because they spend upstream quota and require user keys.

- [ ] **Step 5: Final stale-claim scan**

Run:

```powershell
rg -n "Vertex 一定|Vertex/AI Studio.*拒绝|接受 = OAuth|基本是 OAuth|v1.8" .
```

Expected:
- No stale v1.8 logic in active code.
- `v1.8` may remain only in historical docs or changelog sections.

---

## Self-Review

**Spec coverage:** The plan maps v1.9 scheme changes into shared classifier logic, active audit, manual probe, source probe, reports, Web labels, and docs.

**Placeholder scan:** No task uses TBD-style placeholders. Optional Web test modification is conditional because label-only front-end changes may not affect current server tests.

**Type consistency:** The shared helper returns plain dicts, matching the repository’s current artifact style. Field names used later are defined in Task 1: `classification_version`, `capability`, `signal`, `note`, `oauth_suspect`, `hard_oauth_evidence`, `latency_warning`.
