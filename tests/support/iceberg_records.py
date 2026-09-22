"""Physical observations through the retained Iceberg metadata API."""


def files(storage, reference):
    """Return each physical data file's relative path, record count and byte size."""
    layer = reference if hasattr(reference, 'table') else storage.available(reference)
    return [dict(path=layer.table.io.path(task.file.file_path).relative_to(storage.root).as_posix(),
                 recordCount=task.file.record_count, byteSize=task.file.file_size_in_bytes)
            for task in layer.table.scan().plan_files()]
