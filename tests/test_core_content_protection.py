"""Cross-process protection for new/reused content, publication and cleanup."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import subprocess
import sys
from threading import Barrier

import pytest

from docspec.adapters.storage.ledger import LocalSqliteCoreLedger
from docspec.domain.core import State
from docspec.errors import StateTransitionError
from docspec.ports.core_ledger import MetadataBatch


def test_shared_publications_coexist_and_cleanup_is_exclusive(tmp_path):
    path = tmp_path / "ledger.sqlite"
    with closing(LocalSqliteCoreLedger(path)) as ledger, closing(LocalSqliteCoreLedger(path)) as other:
        barrier = Barrier(2)

        def publish(index):
            with ledger.content_guard():
                barrier.wait(timeout=5)
                return ledger.commit(MetadataBatch(str(index), records=(State(format_version=1, state_id=str(index)),)))

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(publish, range(2))) == [True, True]
        with ledger.content_guard():
            with pytest.raises(StateTransitionError, match="protected"):
                with other.content_guard(exclusive=True):
                    pytest.fail("cleanup entered a live publication")
            with pytest.raises(StateTransitionError, match="upgrade"):
                with ledger.content_guard(exclusive=True):
                    pytest.fail("unsafe lock upgrade")
        with ledger.content_guard(exclusive=True):
            with ledger.content_guard(exclusive=True):
                pass
            with pytest.raises(StateTransitionError, match="protected"):
                other.commit(MetadataBatch("blocked"))
        assert other.commit(MetadataBatch("blocked"))


def test_kernel_releases_protection_when_publisher_process_dies(tmp_path):
    path = tmp_path / "ledger.sqlite"
    with closing(LocalSqliteCoreLedger(path)) as ledger:
        code = """
import os, sys
from pathlib import Path
from docspec.adapters.storage.ledger import LocalSqliteCoreLedger
ledger = LocalSqliteCoreLedger(Path(sys.argv[1]), create=False)
with ledger.content_guard():
    print('protected', flush=True)
    sys.stdin.readline()
    os._exit(19)
"""
        process = subprocess.Popen([sys.executable, "-c", code, str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert process.stdout.readline() == "protected\n"
            with pytest.raises(StateTransitionError, match="protected"):
                with ledger.content_guard(exclusive=True):
                    pytest.fail("cleanup entered another process's publication")
            process.communicate("exit\n", timeout=10)
            assert process.returncode == 19
            with ledger.content_guard(exclusive=True):
                pass
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
