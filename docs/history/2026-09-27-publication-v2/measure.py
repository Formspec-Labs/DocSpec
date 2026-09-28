"""Measure publication-v2 admission on real spicy-regs generations (this directory's README).

Run from the DocSpec checkout with its own runner, one step per process so each
admission's time and peak RSS are its own; steps over 100k rows run under
pm01's ``watch.sh LOG 12 -- ...``:

    uv run --frozen --extra dagster --extra s3 python docs/history/2026-09-27-publication-v2/measure.py STEP ...

Steps:
  fetch ROOT FAMILY DIGEST        download a pinned family from the public bucket into ROOT/mirror
  bases ROOT FIXTURE PUBLISHER    lay out local publication bases: APFS clones of the generations, the
                                  live version-1 pointer, a version-2 pointer the publisher's code derives
                                  from it, and the publisher's fixture pointers
  whole ROOT FIXTURE              seal the live single-file bill_sections, with the congress column derived
                                  from bill_id, as a one-file generation (the occurrence-equality base)
  admit BASE FAMILY TABLE WORKSPACE OUT [--dataset D] [--provisional-key]
                                  admit one table into WORKSPACE and write its evidence to OUT
  changes WORKSPACE OLDER NEWER OUT
                                  time changes() alone between two admitted states (their evidence files)
  refusals ROOT OUT               stage lying variants of the documents split; record each refusal
  receipt ROOT OUT PRODUCTION     assemble evidence, time and memory, agreement checks and the production
                                  admission receipt's state IDs into OUT
"""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # the checkout, for tests.support.generations
PUBLIC = "https://pub-72e95c0c20a84508b42b03a6ff6d55f8.r2.dev"
AGENT = "docspec-publication-v2-measurement"
BILL_SECTIONS_KEY = ("bill_id", "version_code", "source", "seq")
# Production pins (DocSpec docs/pins/fork-generations.json) that the live pointer no longer names.
PINNED = {"dockets": "sha256:e601dbf6ac0bc4fbd9856b0b6b5cbc2e0d0ece726543cef5b2c52463c89c8f4e"}


def _get(url, path):
    request = urllib.request.Request(url, headers={"User-Agent": AGENT})
    path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(request, timeout=120) as response, path.open("wb") as output:
        while chunk := response.read(1 << 20):
            output.write(chunk)


def fetch(root, family, digest):
    """Download one pinned family's root, manifest and members; Rulespec admission checks every byte later."""
    prefix = f"generations/{family}/{digest.removeprefix('sha256:')}"
    target = Path(root) / "mirror" / prefix
    for key in ("artifact.json", "members.json"):
        _get(f"{PUBLIC}/{prefix}/{key}", target / key)
    for member in json.loads((target / "members.json").read_bytes())["members"]:
        if not (target / member["objectKey"]).exists():
            _get(f"{PUBLIC}/{prefix}/{member['objectKey']}", target / member["objectKey"])


def _clone(source, target):
    """An APFS copy-on-write clone: DocSpec never follows links, and the source stays untouched."""
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cp", "-c", "-R", str(source), str(target)], check=True)


def bases(root, fixture, publisher):
    """Local publication bases over the same generation bytes.

    ``live-v1`` serves the public ``publication.json`` fetched now, alone.
    ``live-v2`` adds ``publication.v2.json`` as the publisher's bootstrap writes
    it from version 1 (``{**parse_index(v1), "version": 2}``). ``fixture-v2``
    and ``fixture-v1`` serve the publisher's fixture pointers, v2 with its
    derived v1 and v1 alone. ``publisher`` is spicy-regs' ``publication.py``
    from ``multifile-builder``, extracted with ``git show``.
    """
    root, fixture = Path(root), Path(fixture)
    sys.modules.setdefault("loguru", SimpleNamespace(logger=SimpleNamespace(info=print, error=print, warning=print)))
    spec = __import__("importlib.util").util.spec_from_file_location("publisher", publisher)
    module = __import__("importlib.util").util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from rulespec_artifacts import canonical_json_bytes
    _get(f"{PUBLIC}/publication.json", root / "live-publication.json")
    live = (root / "live-publication.json").read_bytes()
    index = module.parse_index(live)
    generations = {}
    for directory in (root / "mirror" / "generations", fixture / "generations"):
        for family in directory.iterdir():
            for generation in family.iterdir():
                generations[f"generations/{family.name}/{generation.name}"] = generation
    # The production dockets pin, no longer in the live pointer, entered the way the publisher enters a family.
    families = dict(index["families"])
    for family, digest in PINNED.items():
        prefix = f"generations/{family}/{digest.removeprefix('sha256:')}"
        artifact = json.loads((generations[prefix] / "artifact.json").read_bytes())
        members = [SimpleNamespace(object_key=member["objectKey"], sha256=member["sha256"], byte_size=member["byteSize"],
                                   record_count=member["recordCount"])
                   for member in json.loads((generations[prefix] / "members.json").read_bytes())["members"]]
        families[family] = {"prefix": prefix, "logicalId": artifact["logicalId"], "artifactDigest": digest,
                            "tables": module.table_entries(artifact["spec"]["tables"], members)}
    pinned = {**index, "families": families}
    pointers = {"live-v1": {"publication.json": live},
                "pins-v1": {"publication.json": canonical_json_bytes(pinned)},
                "pins-v2": {"publication.json": canonical_json_bytes(pinned),
                            "publication.v2.json": canonical_json_bytes({**pinned, "version": 2})},
                "live-v2": {"publication.json": live,
                            "publication.v2.json": canonical_json_bytes({**index, "version": 2})},
                "fixture-v2": {name: (fixture / name).read_bytes() for name in ("publication.json", "publication.v2.json")},
                "fixture-v1": {"publication.json": (fixture / "publication.json").read_bytes()}}
    for name, files in pointers.items():
        base = root / "bases" / name
        for prefix, source in generations.items():
            if not (base / prefix).exists():
                _clone(source, base / prefix)
        for key, payload in files.items():
            module.parse_index(payload)  # the publisher's own reader accepts every pointer served here
            (base / key).write_bytes(payload)
    print(json.dumps({name: sorted(files) for name, files in pointers.items()}))


def whole(root, fixture):
    """Seal the live bill_sections as one file with ``congress`` derived from bill_id, as the split members carry it.

    The column is derived here with DuckDB from the live file, independently of
    the publisher's builder, and appended last as the builder appends it. Row
    groups stay far below DocSpec's 256 MiB bound.
    """
    import duckdb
    from rulespec_artifacts import LocalMemberSource, describe_member
    from tests.support.generations import _described_columns, seal_generation
    live = next((Path(fixture) / "generations" / "bill-family").glob("72899ab3*")) / "bill_sections.parquet"
    source = Path(root) / "whole"
    source.mkdir(parents=True)
    with duckdb.connect() as connection:
        connection.execute(f"COPY (SELECT *, split_part(bill_id, '-', 1) AS congress FROM read_parquet('{live}', "
                           f"hive_partitioning = false)) TO '{source / 'bill_sections.parquet'}' "
                           "(FORMAT parquet, ROW_GROUP_SIZE 50000)")
        rows = connection.execute(f"SELECT count(*) FROM '{source / 'bill_sections.parquet'}'").fetchone()[0]
    member = describe_member(LocalMemberSource(source), object_key="bill_sections.parquet", role="table",
                             media_type="application/vnd.apache.parquet", record_count=rows)
    description = {"columns": _described_columns(source / member.object_key), "rows": rows}
    pin = seal_generation(source, "bill-family", [member], {member.object_key: description})
    print(json.dumps({"pin": pin.artifact_digest, "sha256": member.sha256, "rows": rows}))


def provisional_bill_sections_key():
    """Measurement only: the key these measurements used for bill_sections, before DocSpec compiled one.

    spicy-docs declared none for the composite identity before 0.46.0 (ruling R6); 0.46.0 declares at-joined/1,
    which DocSpec compiles since 0.12.1. This one keeps the recorded numbers reproducible.

    This spells (bill_id, version_code, source, seq) as a canonical JSON array
    in-process, so the admission path can be timed over real rows; what it
    mints stays in this candidate directory.
    """
    from spicy_docs.schemas import TABLE_CONTRACTS
    from spicy_docs.schemas.tables import KEY_SPELLINGS
    from docspec.adapters import generation_source
    from docspec.adapters.storage import table_sql
    from docspec.domain.identity import canonical_value_bytes
    name = "measurement-json-array/1"
    registry = {**KEY_SPELLINGS, name: lambda values: canonical_value_bytes(list(values)).decode()}
    generation_source.KEY_SPELLINGS = table_sql.KEY_SPELLINGS = registry
    generation_source.TABLE_CONTRACTS = {**TABLE_CONTRACTS, "bill_sections": SimpleNamespace(
        identity=BILL_SECTIONS_KEY, key_spelling=name, types=None)}
    table_sql._SPELLINGS[("measurement-json-array", "1")] = table_sql._Spelling(
        BILL_SECTIONS_KEY, (frozenset({"VARCHAR"}),) * len(BILL_SECTIONS_KEY), lambda parts: table_sql.json_array_sql(*parts),
        table_sql._producer(name), lambda key: ())


def _stream_digest(relation, columns):
    digest, rows = hashlib.sha256(), 0
    with closing(relation.project(", ".join(columns)).order(columns[0]).to_arrow_reader(100_000)) as reader:
        for batch in reader:
            for values in zip(*(batch.column(name).to_pylist() for name in columns)):
                digest.update(b"\t".join(value if isinstance(value, bytes) else value.encode() for value in values) + b"\n")
                rows += 1
    return {"rows": rows, "sha256": "sha256:" + digest.hexdigest()}


def admit(base, family, table, workspace_path, out, dataset=None, provisional=False):
    """Admit one table, then record what any workspace admitting it must reproduce."""
    if provisional:
        provisional_bill_sections_key()
    from docspec.domain.references import LayerRef
    from docspec.runtime import CoreWorkspace
    evidence = {"base": str(base), "family": family, "table": table, "dataset": dataset}
    with CoreWorkspace(Path(workspace_path)) as workspace:
        base_state = None if dataset is None else workspace.ledger.current(dataset)
        started = time.monotonic()
        admitted = workspace.admit_generation(base, family=family, table=table, dataset=dataset)
        evidence["seconds"] = round(time.monotonic() - started, 2)
        evidence.update(state_id=admitted.state_id, report=admitted.report)
        with workspace.publisher.session() as session:
            manifest = workspace.states.manifest(session, admitted.state_id)
        layers = {name: LayerRef.from_dict(manifest[name]) for name in ("table", "membership")}
        records = workspace.records
        sealed = {ref.locator: ref.digest for ref in records.physical_references(layers["table"])}
        evidence["table"] = {"memberDigest": records.admitted(layers["table"]).member_digest,
                             "files": sorted(sealed[locator] for locator in records.data_files(layers["table"])),
                             "layerDigest": layers["table"].digest}
        with workspace.open_state(admitted.state_id) as reader, reader.table() as rows:
            evidence["occurrences"] = _stream_digest(rows, ["member_key", "occurrence_id"])
        with records.relations({"membership": layers["membership"]}) as relations:
            evidence["membership"] = _stream_digest(relations["membership"], ["record_identity", "record_json"])
        if base_state is not None:
            with workspace.open_state(base_state[1]) as older, workspace.open_state(admitted.state_id) as newer:
                evidence["changes"] = sum(1 for _ in newer.changes(older))
    Path(out).write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(json.dumps({key: evidence[key] for key in ("state_id", "seconds", "occurrences")}))


def changes(workspace_path, older, newer, out):
    """Time ``changes()`` alone between two admitted states named by their evidence files; count what it reports."""
    provisional_bill_sections_key()  # reading bill_sections states spells their keys; other tables are unaffected
    from docspec.runtime import CoreWorkspace
    states = [json.loads(Path(path).read_text())["state_id"] for path in (older, newer)]
    with CoreWorkspace(Path(workspace_path)) as workspace, workspace.open_state(states[0]) as before, \
            workspace.open_state(states[1]) as after:
        started = time.monotonic()
        reported = sum(1 for _ in after.changes(before))
        seconds = round(time.monotonic() - started, 2)
    result = {"older": states[0], "newer": states[1], "reported": reported, "seconds": seconds}
    Path(out).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


def refusals(root, out):
    """Lying inputs over the real documents split, each refused at staging before anything is registered.

    Each case is an APFS clone of ``bases/documents-split`` with one change;
    the resealed case rewrites one member and reseals the artifact with
    Rulespec, so only DocSpec's own partition check can refuse it.
    """
    import pyarrow.parquet as pq
    from rulespec_artifacts import LocalMemberSource, MemberDescriptor, canonical_json_bytes, describe_member
    from docspec.adapters.generation_source import stage_generation
    from docspec.errors import IntegrityError
    from tests.support.generations import seal_generation
    root = Path(root)
    source = root / "bases" / "documents-split"
    pointer = json.loads((source / "publication.v2.json").read_bytes())
    entry = pointer["families"]["documents"]
    prefix = entry["prefix"]

    def rewrite(base, change):
        value = json.loads(json.dumps(pointer))
        change(value["families"]["documents"]["tables"]["documents.parquet"])
        (base / "publication.v2.json").write_bytes(canonical_json_bytes(value))

    def outside(table):
        table["members"][0]["key"] = "../" + table["members"][0]["key"]

    def twice(table):
        table["members"].append(dict(table["members"][0]))
        table["rows"] += table["members"][0]["rows"]
        table["byteSize"] += table["members"][0]["byteSize"]

    def reseal(base):
        """The EPA member replaced by ten FDA rows, rewritten by pyarrow; manifest and root resealed to match."""
        directory = base / prefix
        epa, fda = (directory / "documents" / f"agency_code={code}" / "part-000000.parquet" for code in ("EPA", "FDA"))
        rows = pq.read_table(fda, partitioning=None).slice(0, 10)
        epa.unlink()
        pq.write_table(rows, epa)
        members = [describe_member(LocalMemberSource(directory), object_key=raw["objectKey"], role="table",
                                   media_type=raw["mediaType"], record_count=rows.num_rows)
                   if raw["objectKey"].endswith("agency_code=EPA/part-000000.parquet")
                   else MemberDescriptor.from_dict(raw, path=str(directory / raw["objectKey"]))
                   for raw in json.loads((directory / "members.json").read_bytes())["members"]]
        tables = json.loads((directory / "artifact.json").read_bytes())["spec"]["tables"]
        tables["documents.parquet"]["rows"] = sum(member.record_count for member in members)
        seal_generation(directory, "documents", members, tables)
        (base / "publication.v2.json").unlink()  # admit the resealed generation directly
        return directory

    cases = {
        "version-1 pointer only (derive_v1 omits the all-split family)": lambda base: (base / "publication.v2.json").unlink(),
        "a member outside the family prefix": lambda base: rewrite(base, outside),
        "a member listed twice": lambda base: rewrite(base, twice),
        "rows not summing": lambda base: rewrite(base, lambda table: table.update(rows=table["rows"] + 1)),
        "a partition value that disagrees with the member's rows": reseal,
    }
    result = {}
    for name, change in cases.items():
        base = root / "refusals" / name.split(" (")[0].replace(" ", "-").replace("'", "")
        base.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["cp", "-c", "-R", str(source), str(base)], check=True)
        target = change(base) or base
        started = time.monotonic()
        try:
            with stage_generation(target, family="documents", table="documents", directory=root / "refusals"):
                result[name] = {"admitted": True}
        except IntegrityError as error:
            result[name] = {"refused": str(error), "seconds": round(time.monotonic() - started, 2)}
    Path(out).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


# Pairs that must agree, and the reason they must.
SAME = {
    "v1 and v2 pointers, one file each": [("fr-v1", "fr-v2"), ("dockets-pin-v1", "dockets-pin-v2"),
                                          ("dockets-v1", "dockets-v2"), ("documents-v1", "documents-v2"),
                                          ("bills-v1", "bills-v2")],
    "live v1 and the publisher's fixture v2, same pins": [("fr-v1", "fr-fixture-v2"), ("dockets-v1", "dockets-fixture-v2"),
                                                          ("documents-v1", "documents-fixture-v2")],
    "two version-1 workspaces (the baseline)": [("fr-v1", "fr-v1-again")],
    "HTTPS falling back to version 1 against local version 1": [("fr-v1", "fr-https")],
    "congress_bills from the live family (v1) and from the mixed split family (v2)": [("bills-v1", "bills-fixture-v2")],
    "bill_sections as one live-derived file and split, each admitted first": [("sections-whole", "sections-split-first")],
    "documents as the live file and split by agency, each admitted first": [("docsingle-first", "docsplit-first")],
    "documents admitted first with and without a dataset": [("documents-v1", "docsingle-first")],
}


def _log(path):
    """Wall seconds and peak RSS of one watched step, from /usr/bin/time -l and watch.sh's tree samples."""
    text, meta = Path(path).read_text(), Path(str(path) + ".meta").read_text()
    real = next(line for line in text.splitlines() if " real " in line).split()[0]
    rss = next(line for line in text.splitlines() if "maximum resident set size" in line).split()[0]
    return {"wallSeconds": float(real), "maxRssGiB": round(int(rss) / 1024**3, 2),
            "watchPeakTreeRssGiB": round(int(meta.rsplit("peak_tree_rss_kib ", 1)[1]) / 1024**2, 2)}


def receipt(root, out, production):
    """Assemble every step's evidence, time and memory, the agreement checks and the production cross-check."""
    root, runs = Path(root), {}
    for path in sorted((root / "evidence").glob("*.json")):
        if path.stem.split("-", 1)[0] in ("changes", "refusals"):
            runs[path.stem] = {**json.loads(path.read_text()), **_log(root / "logs" / f"{path.stem}.log")}
            continue
        evidence = json.loads(path.read_text())
        report = evidence["report"]
        runs[path.stem] = {"stateId": evidence["state_id"], "pin": report["pin"]["artifactDigest"],
                           "members": [member["sha256"] for member in report.get("members", [report.get("member")])],
                           "partitionColumns": report.get("partitionColumns"), "counts": report["counts"],
                           "changesFromBase": evidence.get("changes"), "admitSeconds": evidence["seconds"],
                           "occurrences": evidence["occurrences"], "membership": evidence["membership"],
                           "table": evidence["table"], **_log(root / "logs" / f"{path.stem}.log")}
    fields = ("stateId", "pin", "members", "counts", "occurrences", "membership", "table.files", "table.memberDigest",
              "table.layerDigest")

    def value(run, name):
        for part in name.split("."):
            run = run[part]
        return run
    agreement = {reason: {f"{a} = {b}": [name for name in fields if value(runs[a], name) != value(runs[b], name)]
                          for a, b in pairs if a in runs and b in runs} for reason, pairs in SAME.items()}
    admitted = json.loads(Path(production).read_text())["sources"]
    crosscheck = {name: {"production": admitted[source]["state_id"], "here": runs[name]["stateId"],
                         "equal": admitted[source]["state_id"] == runs[name]["stateId"]}
                  for name, source in (("fr-v2", "federal-register"), ("fr-https", "federal-register"),
                                       ("dockets-pin-v2", "dockets")) if name in runs}
    split = {}
    for name, run in runs.items():
        if run.get("partitionColumns"):
            evidence = json.loads((root / "evidence" / f"{name}.json").read_text())["report"]
            split[name] = {"members": len(run["members"]),
                           "memberSetEqualsRegisteredFiles": sorted(run["members"]) == run["table"]["files"],
                           "memberRowsSumToTable": sum(member["recordCount"] for member in evidence["members"])
                           == run["counts"]["rows"]}
    # Only these fields are compared; every layer reference, the read pin and the representation's membership
    # digest and locator differ between any two workspaces too (README, "Baseline").
    summary = {"agreementFields": list(fields), "differingFields": agreement, "productionStateIds": crosscheck,
               "splitTables": split}
    Path(out).write_text(json.dumps({"runs": runs, **summary}, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    step, *arguments = sys.argv[1:]
    if step == "admit":
        options = {"provisional": "--provisional-key" in arguments}
        arguments = [argument for argument in arguments if argument != "--provisional-key"]
        if "--dataset" in arguments:
            index = arguments.index("--dataset")
            options["dataset"] = arguments[index + 1]
            del arguments[index:index + 2]
        admit(*arguments, **options)
    else:
        {"fetch": fetch, "bases": bases, "whole": whole, "changes": changes, "refusals": refusals,
         "receipt": receipt}[step](*arguments)
