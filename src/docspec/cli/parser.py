"""Commands over the shared Core runtime and source-catalog tooling."""

import argparse
import importlib
from pathlib import Path

from rulespec_artifacts import Producer

from docspec.adapters.content_fetchers.local_file import LocalFileContentFetcher
from docspec.cli.source_catalog import add_source_catalog_command
from docspec.cli_io import CliError, emit, emit_error, read_bytes
from docspec.domain import core
from docspec.domain.content import SourceItem
from docspec.domain.core_admission import admit_record, encode_record, record_value
from docspec.domain.identity import parse_closed_json, thaw_json
from docspec.domain.streams import owned_iterator
from docspec.errors import DocSpecError
from docspec.ports.record_storage import BATCH_BYTES
from docspec.runtime.core import CoreWorkspace


def _json_rows(path):
    with Path(path).open("rb") as source:
        while payload := source.readline(BATCH_BYTES + 1):
            if len(payload) > BATCH_BYTES:
                raise CliError("JSON row exceeds the 8 MiB input limit")
            if payload.strip():
                yield thaw_json(parse_closed_json(payload, label="input row"))


def _record(path):
    value = thaw_json(parse_closed_json(read_bytes(path, label="Core record", max_bytes=BATCH_BYTES)))
    return admit_record(encode_record(value))


def _keyed_rows(path):
    with owned_iterator(_json_rows(path)) as source:
        for row in source:
            if not isinstance(row, dict) or set(row) != {"key", "value"}:
                raise CliError("state input rows require exactly key and value")
            yield row["key"], row["value"]


def _producer(name):
    module, separator, attribute = name.partition(":")
    if not separator or not module or not attribute:
        raise CliError("producer must be an importable module:function")
    value = getattr(importlib.import_module(module), attribute)
    if not callable(value):
        raise CliError("producer must be callable")
    return value


def _run(args):
    with CoreWorkspace(args.workspace) as workspace:
        command = args.action
        if command == "create":
            result = record_value(workspace.create(args.state, _keyed_rows(args.rows)))
        elif command == "upsert":
            result = record_value(workspace.upsert(args.base, _keyed_rows(args.rows), batch_id=args.batch, dataset=args.dataset))
        elif command == "revise":
            result = record_value(workspace.revise(_record(args.revision)))
        elif command == "rows":
            with owned_iterator(workspace.rows(args.state)) as rows:
                for key, entity in rows:
                    emit({"key": key, "entity": record_value(entity)})
            return 0
        elif command == "inspect":
            result = workspace.inspect(args.kind, args.id, progress_limit=args.limit)
        elif command == "compare":
            result = workspace.compare(args.older, args.newer, sample_limit=args.limit)
        elif command == "retain":
            with owned_iterator(_json_rows(args.records)) as rows:
                result = {"committed": workspace.retain(rows, unit_id=args.unit,
                                                          roots=(tuple(key) for key in args.root))}
        elif command == "select":
            result = {"changed": workspace.maintenance.select_current(args.unit, args.dataset, tuple(args.target),
                         None if args.expected is None else tuple(args.expected)), "current": list(workspace.ledger.current(args.dataset))}
        elif command == "remove":
            workspace.maintenance.remove_under_policy(args.unit, args.policy, (tuple(key) for key in args.key))
            result = {"removal_id": args.unit, "complete": workspace.ledger.removal(args.unit)[2]}
        elif command == "resume-removal":
            workspace.maintenance.resume(args.unit)
            result = {"removal_id": args.unit, "complete": workspace.ledger.removal(args.unit)[2]}
        elif command == "export":
            producer = Producer.from_dict(thaw_json(parse_closed_json(read_bytes(args.producer, label="export producer"))), path="export/producer")
            with owned_iterator(() if args.roots is None else _json_rows(args.roots)) as roots:
                pin = workspace.export(args.state, args.destination, producer=producer, max_output_bytes=args.max_bytes,
                                       additional_roots=(tuple(key) for key in roots))
            result = pin.as_dict()
        elif command == "execute":
            result = workspace.operations.resolve(_record(args.definition), _record(args.request), _producer(args.producer),
                selection_id=args.selection, target=core.Origin(parent_entity_id=args.target),
                reuse_policy=lambda prior: prior.outcome.status == "success", fresh=args.fresh)
            from docspec.application.core_execution import SuspendedOperation
            if isinstance(result, SuspendedOperation):
                result = {"status": "suspended", "execution_id": result.execution_id}
            else:
                result = {"selection": record_value(result.selection), "result": record_value(result.result)}
        elif command == "document-import":
            pipeline = workspace.documents(fetcher=LocalFileContentFetcher(args.input_root))
            with owned_iterator(_json_rows(args.sources)) as rows:
                result = record_value(pipeline.import_sources((SourceItem.from_dict(row) for row in rows), state_id=args.state))
        elif command == "document-run":
            pipeline = workspace.documents(fetcher=LocalFileContentFetcher(args.input_root))
            result = record_value(pipeline.run(args.source_state, run_id=args.run_id, dataset=args.dataset,
                extract=args.stop_after != "capture", segment=args.stop_after == "segmentation", fresh=args.fresh))
        else:
            raise CliError("unsupported Core action")
        emit(result)
        return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="docspec", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    add_source_catalog_command(commands)

    def command(group, name, help_text, *, action=None):
        child = group.add_parser(name, help=help_text)
        child.add_argument("--workspace", type=Path, required=True)
        child.set_defaults(func=_run, action=action or name)
        return child

    states = commands.add_parser("state", help="Create, revise, and read keyed values").add_subparsers(required=True)
    create = command(states, "create", "Import streamed JSON lines containing key and value")
    create.add_argument("--rows", type=Path, required=True)
    create.add_argument("--state", required=True)
    revise = command(states, "revise", "Apply a Core revision")
    revise.add_argument("--revision", type=Path, required=True)
    upsert = command(states, "upsert", "Add or replace keyed JSON values with safe batch retries")
    upsert.add_argument("--rows", type=Path, required=True)
    upsert.add_argument("--base", required=True)
    upsert.add_argument("--batch", required=True, help="Stable identity for this exact ordered input and base")
    upsert.add_argument("--dataset", help="Advance this dataset only if its current state is the base")
    rows = command(states, "rows", "Stream keyed occurrence records")
    rows.add_argument("--state", required=True)

    inspect = command(commands, "inspect", "Read history, availability, requests and exact selections")
    inspect.add_argument("--kind", choices=tuple(core.RECORD_ID_FIELDS), required=True)
    inspect.add_argument("--id", required=True)
    inspect.add_argument("--limit", type=int, default=20)
    compare = command(commands, "compare", "Count state changes with a bounded sample")
    compare.add_argument("older")
    compare.add_argument("newer")
    compare.add_argument("--limit", type=int, default=20)
    retain = command(commands, "retain", "Retain a bounded Core metadata unit")
    retain.add_argument("--records", type=Path, required=True)
    retain.add_argument("--root", nargs=2, action="append", metavar=("KIND", "ID"), required=True)
    retain.add_argument("--unit", required=True)
    select = command(commands, "select", "Change current only if its previous value still matches")
    select.add_argument("--dataset", required=True)
    select.add_argument("--target", nargs=2, metavar=("KIND", "ID"), required=True)
    select.add_argument("--expected", nargs=2, metavar=("KIND", "ID"))
    select.add_argument("--unit", required=True)
    remove = command(commands, "remove", "Remove bytes under a retained explicit policy")
    remove.add_argument("--policy", required=True)
    remove.add_argument("--key", nargs=2, action="append", metavar=("KIND", "ID"), required=True)
    remove.add_argument("--unit", required=True)
    resume = command(commands, "resume-removal", "Resume an interrupted authorized removal")
    resume.add_argument("--unit", required=True)
    exported = command(commands, "export", "Export a selected state and explicit evidence roots")
    exported.add_argument("--state", required=True)
    exported.add_argument("--destination", type=Path, required=True)
    exported.add_argument("--producer", type=Path, required=True)
    exported.add_argument("--max-bytes", type=int, required=True)
    exported.add_argument("--roots", type=Path, help="JSON lines of [kind, id] pairs for additional retained evidence")
    execute = command(commands, "execute", "Resolve a Core request through the shared operation lifecycle")
    for name in ("definition", "request"):
        execute.add_argument("--" + name, type=Path, required=True)
    for name in ("producer", "selection", "target"):
        execute.add_argument("--" + name, required=True)
    execute.add_argument("--fresh", action="store_true")

    documents = commands.add_parser("document", help="Capture, extract and segment through Core").add_subparsers(required=True)
    imported = command(documents, "import", "Import source item JSON lines", action="document-import")
    imported.add_argument("--sources", type=Path, required=True)
    imported.add_argument("--state", required=True)
    imported.add_argument("--input-root", type=Path, required=True)
    run = command(documents, "run", "Process retained source items", action="document-run")
    run.add_argument("--source-state", required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--input-root", type=Path, required=True)
    run.add_argument("--dataset")
    run.add_argument("--stop-after", choices=("capture", "extraction", "segmentation"), default="segmentation")
    run.add_argument("--fresh", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (DocSpecError, OSError, TypeError, ValueError, ImportError, AttributeError) as error:
        return emit_error(error)
