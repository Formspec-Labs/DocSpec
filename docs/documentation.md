# Documentation ownership

Maintainers edit `README.md`, `CONTRIBUTING.md`, and the guides directly under
`docs/`. `docs/architecture.md` is the current behavior entry point. Update its
links and the contributor task map when code moves.

`wiki/` is generated reference material. Its last recorded generation is
**2026-09-03**, from revision
`0a41ac7fb50f0f0044738b4d7fdf2f2e1b8b5cf7`, according to
[`wiki/metadata.json`](../wiki/metadata.json). That metadata records a past
generation; navigation repairs do not turn it into a new snapshot. Code and
maintained guidance may have changed since then.

Keep wiki generation scoped to `wiki/`. After regeneration, retain a visible
snapshot notice linking to the maintained architecture and contributor guides,
check relative links, and record the new source revision and generation date in
metadata. The repository currently contains generated output, not a checked-in
command that reproduces its external generator. Do not invent a regeneration
command or overwrite maintained guides as part of that process.

`docs/decisions/` retains accepted decisions, amendments, dissent, and their
recorded evidence. Use its [index](decisions/README.md) to distinguish current
rules from historical statements. `docs/history/` retains measurements and
incidents; those reports describe their recorded inputs and revisions, not
automatic evidence about today's code or a later deployment.
