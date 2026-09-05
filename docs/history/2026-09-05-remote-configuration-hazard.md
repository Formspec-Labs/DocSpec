# `git push` in DocSpec goes to origin at Formspec-Labs — which is the intended destination

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

## RESOLVED the same day: origin is correct

Mike, relayed 2026-09-05: *"docspec doesn't need a fork, we own formspec labs"*.

So `Formspec-Labs/DocSpec` is not an outside organization and `origin` is the
intended destination; the fork is a backup. The suggestion to repoint `main` at
`fork/main` was withdrawn, and the tracking branch correctly stays on `origin`.

**That inverts the hazard but does not remove it.** The risk was never that
`origin` is wrong — it is that the description and the configuration disagreed,
and a reader acting on either without checking would have been surprised. Had I
followed the instruction to repoint at the fork, DocSpec's default would now
point at a backup instead of at the destination its owner intends, and the 29
commits would be further from where they belong rather than closer. The
explicit-remote rule below stands for exactly that reason: the answer changed
within the hour, and the check does not.

## What was deliberately NOT done

Repointing `main` at `fork/main` was suggested and declined. It is a one-command,
reversible, local change that would make the accident impossible, and the
argument for it is good. Two reasons to leave it for the repository's owner:

1. **It encoded an answer to a question nobody had settled** — which remote is
   authoritative. The configuration said `origin`; recent practice said `fork`.
   Changing the config to match practice would have asserted that practice was
   correct, and the assertion would have been mine, made on a peer's reading
   that was demonstrably wrong about this exact repository ten minutes earlier.
2. **Someone might intend to publish here.** `origin` being 29 commits behind
   read equally as "the fork is the working copy" and as "publication is
   overdue".

Both held, and the owner's answer was the second: publication is overdue.
Declining to change the default was right for a reason worth keeping — the
question belonged to the owner, and both available readings were wrong until he
answered. Naming the remote explicitly cost nothing in the meantime and was
correct under either answer, which is the property to look for when a default
is ambiguous and the clock is running.
