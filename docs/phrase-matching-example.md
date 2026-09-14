# An optional processor that finds review passages

`examples/phrase_match_processor.py` implements a bounded segment callback
using installed public DocSpec types. It stays example-scoped: applications may
adapt it, and DocSpec does not install a phrase-classification service or resource
registry. The [offline walkthrough](offline-walkthrough.md) shows its full lifecycle.

The constructor accepts a Core `Resource`, exact JSON bytes, optional `case_sensitive`,
and `ProcessorLimits`. It verifies the resource's SHA-256 before parsing and stores
an immutable snapshot. No file or network resource is loaded during processing.

```json
{"terms":[{"id":"personal-data","label":"Personal data mentions","phrases":["personal data"]}]}
```

Only `terms`, and each term's `id`, `label`, and `phrases`, are accepted. IDs and
labels are nonempty; term IDs and phrases within one term must be distinct.
Different terms may deliberately share a phrase. A resource contains at most
64 KiB of JSON, 64 terms, 256 phrases, and 128 Unicode characters per phrase.
These bounds and matching settings contribute to the processor's configuration
digest and are defined in this guide and the example source. The `provider_processor` adapter pins the limits, settings and Core resource.

Phrases are escaped literals. They are not regular expressions. A match is
accepted when the adjacent characters, if any, are neither Unicode letters or
digits nor underscores. Matching ignores case by default using Python's Unicode
`re.IGNORECASE` rules; case-sensitive mode compares original characters exactly.
No Unicode normalization, stemming, whitespace folding or semantic expansion is
performed. The matcher checks every start position, retaining overlaps and each
matching phrase/term. Results sort by byte start, byte end, term ID, then phrase.

Each UTF-8 text segment returns a `ProcessorResponse` with one output value. `matches: []` records a
successful no-match result. Every match has the literal phrase, term ID/label,
original quote, and half-open byte range in the segment. The result carries the
segment ID/digest, exact reference-data identity and unchanged enclosing source
evidence. Segment match offsets do not claim to be captured-file offsets.

By default, an invocation allows one segment of at most 64 KiB, one output record
of at most 64 KiB, and five seconds. The matcher also refuses more than 1,024
matches. It refuses overflow instead of silently truncating, and ordinary runtime
admission verifies output identity, resource use and evidence references. Reported
resource use is local bytes and measured time; there are no external requests or
claims about monetary cost.

Changing the actual matching configuration or resource identity changes the
Core operation definition. The shared evaluator can then reuse compatible
captures, representations and segments and reruns this processor. It does not
need another configuration ledger. A local supplied vocabulary is reference input,
not proof of a live RefSpec resource or an authoritative domain interpretation.
