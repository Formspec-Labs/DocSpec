# Authored FEC committee fixture

`committees.json` is a synthetic OpenFEC-shaped response containing two invented
committee records. It is not a captured publisher response or evidence about
actual committees. Its API request description and observation time are authored
by [the example](../fec_committees.py); no request is made.

The records exercise nested unknown metadata, non-ASCII text, null and empty
values, cycles, and candidate identifiers. The synthetic PDF link is preserved as
source metadata and an asset offer; it is never requested or promoted to a
document candidate.

The example publishes these exact bytes through SpicyDocs' ordinary retained
committee census API. Its `--empty` case derives an empty response by clearing
the records and setting the declared count and page count to zero. Neither case
establishes publisher-wide coverage or absence.

See the [FEC catalog guide](../../docs/fec-committees.md) for commands, outputs,
source-evidence retention, and bounds.
