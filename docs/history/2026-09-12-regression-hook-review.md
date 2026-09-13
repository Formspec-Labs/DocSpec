# D37 native pytest qualification hook: independent static review

Date: 2026-09-12. Scope: `tests/conftest.py` and `tests/test_regression_gate.py` only, including the final positional-selector and early-collection fixes. The parent owns deletion of the old runtime runner, the current requirement map, CI and documentation. This review does not substitute for review/execution of that wider change. No tests or builds were run by this reviewer.

## 1. Change and decision

**APPROVE the bounded hook.** It observes the existing pytest collection and phase reports, adds no execution loop or saved result format, and leaves ordinary focused pytest unchanged. Exact current requirement selectors must collect, remain selected and complete setup, call and teardown successfully. It preserves native failure exits and only changes an otherwise successful exit when required work is incomplete.

## 2. Function trace

| Function | Location | Inputs → output | Verified behavior |
| --- | --- | --- | --- |
| `pytest_addoption` | `tests/conftest.py:16` | Parser → optional flag | Strict qualification is explicitly enabled, not imposed on every focused developer run. |
| `pytest_configure` | `tests/conftest.py:33` | Native config and local JSON map → small config stash | Rejects node-specific positional selection; validates nonempty map/selector lists and duplicate IDs/selectors; accepts unparameterized function selectors. |
| `pytest_itemcollected` | `tests/conftest.py:64` | Native item → original node ID set | Records parameter cases before ordinary deselection. |
| `pytest_collection_finish` | `tests/conftest.py:69` | Original and selected nodes → required nodes | Exact equality or the same function's `[` suffix binds parameters. Missing selectors and omitted collected cases refuse. |
| `pytest_runtest_makereport` | `tests/conftest.py:87` | Native report → successful phase set or failed sentinel | Any unsuccessful phase or `wasxfail` permanently disqualifies the node. It returns the native report unchanged. |
| `pytest_sessionfinish` | `tests/conftest.py:98` | Required nodes, phase sets and native exit → final native exit | Empty required bookkeeping under an enabled map is incomplete; every node must have exactly setup/call/teardown. Existing nonzero exits survive. |
| `_project` | `tests/test_regression_gate.py:16` | Actual hook source, small test source and map → pytester project | Runs the actual hook through native pytest subprocesses; it does not copy a second hook implementation. |

## 3. State and invariants

The map and original collection live in `config.stash`; nothing is written as a second evidence artifact. Required nodes receive empty phase sets only after complete collection validation. Reports add successful phases; any failure/skip/xfail changes the value to `None`, which later successful phases cannot clear (`tests/conftest.py:90`). Session completion requires the exact three-phase set (`:102`).

The installed pytest's built-in expected-failure report wrapper (`.venv/lib/python3.12/site-packages/_pytest/skipping.py:276`) edits the report before the later-registered conftest wrapper resumes; the native xfail/xpass tests independently exercise that ordering. The local pytest `wrap_session` implementation calls `pytest_sessionfinish` only after session startup completes (`.venv/lib/python3.12/site-packages/_pytest/main.py:317`). This is the existing framework lifecycle, not a process-integrity guarantee supplied by this hook.

## 4. Behavior and edge-case evidence

The inspected native pytester cases cover all parameter cases plus actual JUnit output (`tests/test_regression_gate.py:29`); ordinary focused runs without a map (`:38`); renamed/import-skipped selectors (`:52`); parameter deselection (`:59`); positional parameter selection (`:70`); runtime skip, xfail/xpass, setup skip/error and teardown error (`:89`); collect-only/setup-only (`:97`); early successful exit during a test (`:104`); early successful exit before collection populates requirements (`:111`); and empty/duplicate declarations (`:121`). These are observed framework outcomes, not a fake result dictionary.

The author's initial gate reported 17 passing cases before the final two guards. The final 19-case gate is pending independent execution attribution at this report's creation; no result is inferred from static reading.

## 5. Findings

**Resolved — omitted parameter siblings under positional selection.** Native pytest may collect only the explicitly named parameter, so original-collection bookkeeping alone cannot know its siblings. Strict mode now rejects positional node selectors at `tests/conftest.py:36`; ordinary focused mode remains available. Native regression at `tests/test_regression_gate.py:70` covers the refusal. This is smaller than a second collection pass.

**Resolved — successful collection exit before required population initialization.** Originally `_REQUIRED={}` could look complete to session finish. The enabled-map/empty-required guard at `tests/conftest.py:103` now refuses that case, with a real native collection hook exiting zero at `tests/test_regression_gate.py:111`.

**Remaining qualification limit, not a hook defect:** an exit during native session startup can bypass session-finish hooks altogether. CI must require the actual native JUnit artifact as well as a successful command, and must preserve nonzero pipeline exits. The architecture note records this limit. Do not wrap the whole pytest process or claim this small hook proves execution against arbitrary hostile plugins, deployment failures, dataset capacity or publication.

## 6. Conclusion

**VERDICT: APPROVE.** Static coverage of the bounded two-file change is adequate, and confidence is high. The code pays for its small bookkeeping cost by preventing missing, skipped, partially selected or incompletely executed requirements from producing a normal successful qualification result. Final execution evidence and the CI/map changes remain parent-owned and separate.
