"""Clean-worker composition root for the opt-in Dagster-Celery qualification."""

from __future__ import annotations


def docspec_qualified_celery_job():
    """Reconstruct DocSpec's native job with Dagster's maintained Celery executor."""

    from dagster_celery import celery_executor

    from docspec.adapters.dagster import build_dagster_definitions

    return build_dagster_definitions(executor_def=celery_executor).get_job_def("docspec_store_tasks")
