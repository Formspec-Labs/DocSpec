"""Fresh-process fixture for document budget continuation and abrupt termination.

Run as ``python -m tests.support.document_budget_probe root kind mode run_id limit``;
it prints one JSON line, either ``{"state": run_id}`` or ``{"error": ..., "message": ...}``.
"""

import json
import os
from pathlib import Path
import sys

from docspec.adapters.content_fetchers import LocalFileContentFetcher
from docspec.application.documents import DocumentProcessor
from docspec.domain import core
from docspec.domain.content import CandidateFile, SourceItem
from docspec.ports.content_fetcher import FetchStream
from docspec.runtime import CoreWorkspace


def main():
    root, kind, mode, run_id, limit = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5])
    class Fetcher(LocalFileContentFetcher):
        def fetch(self, candidate, **options):
            with (root / "fetches.txt").open("a") as log:
                log.write(candidate.candidate_id + "\n")
            stream = super().fetch(candidate, **options)
            if kind == "bytes" and candidate.candidate_id == "b" and mode in {"fail", "kill"}:
                def partial():
                    yield b"123"
                    if mode == "kill":
                        os._exit(23)
                    raise RuntimeError("publisher interrupted after three bytes")
                return FetchStream(stream.metadata, partial(), stream.close)
            return stream
    def process(context, inputs):
        with (root / "processors.txt").open("a") as log:
            log.write(mode + "\n")
        context.use(inputs["segments"].state_id)
        if kind == "empty":
            return
        identity = context.execution.execution_id + ":values"
        def rows():
            for index in range(2):
                yield str(index), core.Entity(format_version=1, entity_id=identity + str(index),
                    entity_type="occurrence", value=core.InlineValue(value=index))
            if mode == "fail":
                raise RuntimeError("processor stopped after two rows")
        state = context.session.states.create_keyed(context.session, state_id=identity, representation_id=identity + ":physical",
            unit_id=identity + ":import", rows=rows())
        context.generate_record(state, label="values")
    processor = DocumentProcessor("probe", core.OperationDefinition(format_version=1, definition_id="probe",
        implementation_id="probe", implementation_version="1", operation_kind="transformation", configuration={}), process)
    with CoreWorkspace(root / "workspace") as workspace:
        pipeline = workspace.documents(fetcher=Fetcher(root / "inputs"))
        sources = [SourceItem(name, "1", (CandidateFile(name, name + ".txt", "text/plain"),))
                   for name in ("a", "b") if kind == "bytes" or name == "a"]
        pipeline.import_sources(sources, state_id="source")
        try:
            if mode == "seed":
                pipeline.run("source", run_id="seed")
            else:
                pipeline.run("source", run_id=run_id, max_source_bytes=limit if kind == "bytes" else 0,
                    max_generated_rows=4 if kind == "bytes" else limit,
                    processors=() if kind == "bytes" else (processor,))
        except Exception as error:
            print(json.dumps({"error": type(error).__name__, "message": str(error)}))
        else:
            print(json.dumps({"state": run_id}))


if __name__ == "__main__":
    main()
