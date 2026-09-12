# DocSpec 0.3.0 wheel qualification

The locally built wheel exposes the public runtime, source-catalog, and result
reader APIs. `docspec[spicy-docs]` selects the public SpicyDocs reader without
adding its acquisition or analytics packages. Core DocSpec still installs and
imports without SpicyDocs. The provider depends on Rulespec Artifacts, not
DocSpec, so this introduces no circular dependency.

## Build identity

Source revision: `b89d303c3d97b152d55bf191c4a89d506643424a`.
Build command: `uv build --wheel --out-dir dist`.

| Wheel | SHA-256 |
| --- | --- |
| `docspec-0.3.0-py3-none-any.whl` | `1a06014a4f039b14b5c29dcfb60d8fd83621286ead6bf60162e3e815d5a0d410` |
| `rulespec_artifacts-1.0.12-py3-none-any.whl` | `3f6c946c60ff2ddbe854fce7f74f4358ddb21e3ba3f6ad10caa8a0d8d59fd0a5` |
| `spicy_docs-0.2.0-py3-none-any.whl` | `ecaa5ebc15df7cad12952e5fdeb8e1cef71614471dfb81b5f43316049d246c4e` |

The provider wheel records source revision
`296f20d05e0c32419ebae9d43cdfd19fe054238b`. These identify code packages;
individual source catalogs and retained results have separate data pins.
Rebuilding after later source edits requires recording the new wheel digest.

## Checks and limits

- The package-boundary and installed-source tests passed: **12 tests in 19.26
  seconds**. They cover the isolated core runtime, retained-result export,
  installed public provider reader, source outcomes, and catalog interpretation.
- A separate Python 3.12 environment installed `docspec[spicy-docs]==0.3.0`
  using `--find-links dist --find-links vendor`. `uv pip check` passed for all
  ten installed packages. Isolated imports confirmed both package versions and
  the public runtime, catalog, and result-reader APIs.
- That environment contained no `boto3`, `httpx`, `dagster`, `polars`, `pyarrow`,
  or `pypdf`. Users select those capabilities separately when needed.
- Lock consistency, Ruff, and whitespace checks passed. The previous full-suite
  baseline was 1,071 passing tests at `b1cbc21`; the later packaging changes were
  qualified by the focused checks above.

This is local build and installation evidence. No registry publication, remote
CI, or release deployment is claimed. Independent review of the latest changes
remains open because the reviewers reached their usage limit.

See [installation instructions](../../CONTRIBUTING.md#install-the-optional-source-reader)
and [D45](../dataset-experiments-todo.md#d45).
