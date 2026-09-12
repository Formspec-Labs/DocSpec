# GAO topic handoff inputs

These are the unchanged synthetic GAO pages from SpicyDocs revision
`8e485fe052c794cf18b041debe970c813812c05b`, under `examples/fixtures/gao/`.
They are retained integration inputs, not live GAO captures. Source parsing and
validation remain in the installed provider wheel.

- `matching.html` supplies the literal topic `Information Security`.
- `unexpected.html` supplies `Agency Operations`, which remains a valid source
  observation even when the selected catalog filter does not match it.
- `missing.html` has a navigation topic link but no publisher topic field. The
  provider refuses it and retains the original response for inspection.

DocSpec's example records the input digest, source result and original evidence.
It selects by exact label without interpreting requirements or applicability.
