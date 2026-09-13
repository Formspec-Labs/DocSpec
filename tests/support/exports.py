"""Shared retained-result setup for public export and adversarial admission tests."""

from dataclasses import replace

import pytest

from docspec.runtime import export_local_result
from docspec.result_export import open_result_export

MAX_BYTES = 8 * 1024**2


@pytest.fixture
def export_run(experiment):
    run, retry, fetches, extractor, segmenter = experiment
    settings = {}

    def prepared(value):
        settings.update(plan=value.plan, workspace=value._composition.workspace,
            document_release_producer=value._composition.catalog.producer,
            export_producer=replace(value._composition.catalog.producer,
                verifier_id="urn:docspec:verifier:result-export"))

    def finish(**changes):
        release, view, entries = run(on_prepared=prepared, **changes)
        return release, view, entries, dict(settings)

    return finish, retry, fetches, extractor, segmenter


def export_result(settings, release, destination, *, admission="retained-evidence", **changes):
    return export_local_result(**(settings | changes), release_ref=release,
        destination=destination, admission=admission, max_output_bytes=MAX_BYTES)


def open_export(destination, pin, producer, **changes):
    return open_result_export(destination, expected_pin=pin, producer=producer,
        **({"max_output_bytes": MAX_BYTES} | changes))
