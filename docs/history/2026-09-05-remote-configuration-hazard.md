# `git push` in DocSpec goes to the organization remote, not the fork

Found 2026-09-05 while checking a relayed instruction to push. Recorded because
several sessions work in this repository at once and nothing here says which
remote is which.

## The facts, from the repository rather than from anyone's description

```
origin  https://github.com/Formspec-Labs/DocSpec.git   <- an ORGANIZATION remote
fork    https://github.com/mikewolfd/DocSpec.git
branch.main.remote = origin
remote.pushDefault = (unset)
```

So a bare `git push` on `main` sends to **Formspec-Labs**, not to the fork.

Recent practice points the other way. On the day this was written `fork/main`
was 6 commits behind HEAD and had moved that morning; `origin/main` was **29
commits behind** and last moved the previous day. Work has been going to the
fork while the configured default points at the organization.

## Why it matters

A relayed instruction described pushes in this repository as going "to the
private mikewolfd forks only, as they have all week". That is true of
`spicy-docs`, whose `main` tracks `fork/main`. It is false here, and following
it with a bare `git push` would have put 29 commits on an organization remote.

**Always name the remote explicitly in this repository: `git push fork main`.**
Do not rely on the default, and do not rely on a description of the default —
including this one. Run `git rev-parse --abbrev-ref main@{u}` and look.

## What was deliberately NOT done

Repointing `main` at `fork/main` was suggested and declined. It is a one-command,
reversible, local change that would make the accident impossible, and the
argument for it is good. Two reasons to leave it for the repository's owner:

1. **It encodes an answer to a question nobody has settled** — which remote is
   authoritative. The configuration says `origin`; recent practice says `fork`.
   Changing the config to match practice asserts that practice is correct, and
   the assertion would be mine, made on a peer's reading that was demonstrably
   wrong about this exact repository ten minutes earlier.
2. **Someone may intend to publish here.** `origin` being 29 commits behind is
   equally consistent with "the fork is the working copy" and with "publication
   is overdue". Silently redirecting the default would remove a choice rather
   than make one.

The hazard is real and immediate; the remedy is one word from the owner. Naming
the remote explicitly costs nothing in the meantime and is correct under either
answer.
