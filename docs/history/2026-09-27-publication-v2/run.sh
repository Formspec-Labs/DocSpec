#!/bin/sh
# Every measured step of this directory's receipt, one at a time under pm01's watch.sh (12 GiB cap).
# Run from the DocSpec checkout: sh docs/history/2026-09-27-publication-v2/run.sh ROOT
# ROOT holds bases/ (measure.py bases) and whole/ (measure.py whole); workspaces, evidence and logs land beside them.
set -eu
root=$1
watch=/Users/mikewolfd/Work/corpora/pm01-gate-2026-09-23/tools/watch.sh
measure="/opt/homebrew/bin/uv run --frozen --extra dagster --extra s3 python docs/history/2026-09-27-publication-v2/measure.py"
public=https://pub-72e95c0c20a84508b42b03a6ff6d55f8.r2.dev
mkdir -p "$root/ws" "$root/evidence" "$root/logs"

admit() {  # NAME WORKSPACE BASE FAMILY TABLE [admit options]; a later generation shares its base's workspace
  name=$1; workspace=$2; base=$3; family=$4; table=$5; shift 5
  [ -f "$root/evidence/$name.json" ] && return 0
  $watch "$root/logs/$name.log" 12 -- $measure admit "$base" "$family" "$table" "$root/ws/$workspace" \
    "$root/evidence/$name.json" "$@"
}

# Single-file tables: the live version-1 pointer, the version-2 pointer derived from it, and a second
# version-1 workspace that shows what differs between any two workspaces.
admit fr-v1 fr-v1 "$root/bases/live-v1" federal-register federal_register
admit fr-v2 fr-v2 "$root/bases/live-v2" federal-register federal_register
admit fr-v1-again fr-v1-again "$root/bases/live-v1" federal-register federal_register
admit fr-https fr-https "$public" federal-register federal_register
admit dockets-pin-v1 dockets-pin-v1 "$root/bases/pins-v1" dockets dockets
admit dockets-pin-v2 dockets-pin-v2 "$root/bases/pins-v2" dockets dockets
admit dockets-v1 dockets-v1 "$root/bases/live-v1" dockets dockets
admit dockets-v2 dockets-v2 "$root/bases/live-v2" dockets dockets
admit documents-v1 documents-v1 "$root/bases/live-v1" documents documents
admit documents-v2 documents-v2 "$root/bases/live-v2" documents documents
admit bills-v1 bills-v1 "$root/bases/live-v1" bill-family congress_bills
admit bills-v2 bills-v2 "$root/bases/live-v2" bill-family congress_bills
# The mixed family as the publisher's fixture serves it: congress_bills beside split bill_sections.
admit bills-fixture-v2 bills-fixture-v2 "$root/bases/fixture-v2" bill-family congress_bills
# bill_sections (measurement-only composite key): the live-derived one-file table, then its split successor.
admit sections-whole whole-first "$root/whole" bill-family bill_sections --provisional-key --dataset sections
admit sections-split whole-first "$root/bases/fixture-v2" bill-family bill_sections --provisional-key --dataset sections
# The split admitted first, into a fresh workspace, then the one-file table over it.
admit sections-split-first split-first "$root/bases/fixture-v2" bill-family bill_sections --provisional-key --dataset sections
admit sections-whole-second split-first "$root/whole" bill-family bill_sections --provisional-key --dataset sections
# The live single-file bill_sections, without congress, then the split: the new column re-mints every row.
admit sections-live live-first "$root/bases/live-v1" bill-family bill_sections --provisional-key --dataset sections
admit sections-split-over-live live-first "$root/bases/fixture-v2" bill-family bill_sections --provisional-key --dataset sections
# The publisher's own v2 pointer for the families it did not change: the same pins as live version 1.
admit fr-fixture-v2 fr-fixture-v2 "$root/bases/fixture-v2" federal-register federal_register
admit dockets-fixture-v2 dockets-fixture-v2 "$root/bases/fixture-v2" dockets dockets
admit documents-fixture-v2 documents-fixture-v2 "$root/bases/fixture-v2" documents documents
# documents published split by agency_code with spicy-regs' builder (split_documents.py): 316 members, no new column.
admit docsplit-first docsplit-first "$root/bases/documents-split" documents documents --dataset documents
admit docsingle-over-split docsplit-first "$root/bases/live-v1" documents documents --dataset documents
admit docsingle-first docsingle-first "$root/bases/live-v1" documents documents --dataset documents
admit docsplit-over-single docsingle-first "$root/bases/documents-split" documents documents --dataset documents
# changes() alone, for each successor over its base.
for pair in "whole-first sections-whole sections-split" "live-first sections-live sections-split-over-live" \
    "docsingle-first docsingle-first docsplit-over-single" "docsplit-first docsplit-first docsingle-over-split"; do
  set -- $pair
  [ -f "$root/evidence/changes-$3.json" ] || $watch "$root/logs/changes-$3.log" 12 -- $measure changes "$root/ws/$1" \
    "$root/evidence/$2.json" "$root/evidence/$3.json" "$root/evidence/changes-$3.json"
done
[ -f "$root/evidence/refusals.json" ] || $watch "$root/logs/refusals.log" 12 -- $measure refusals "$root" \
  "$root/evidence/refusals.json"
