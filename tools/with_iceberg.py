"""Run a local command with Apache's REST catalog fixture (requires Docker)."""

from contextlib import suppress
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from http.client import HTTPException
from urllib.request import urlopen
from uuid import uuid4

IMAGE = "apache/iceberg-rest-fixture:1.10.1@sha256:f7d679d30ac9c640bdeb2c015dff533cd3c8f1c7d491ebcb5d436f9a42db1d6f"


def main():
    command = sys.argv[1:]
    if not command:
        raise SystemExit("usage: python tools/with_iceberg.py COMMAND [ARGUMENT ...]")
    if os.environ.get("DOCSPEC_ICEBERG_URI"):
        return subprocess.call(command)
    cache = Path.home() / ".cache/docspec-iceberg"
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="run-", dir=cache) as temporary:
        root = Path(temporary).resolve()
        workspace = Path.cwd().resolve()
        name = "docspec-iceberg-" + uuid4().hex
        try:
            subprocess.run([
                "docker", "run", "--detach", "--rm", "--name", name, "--user", "0",
                "--publish", "127.0.0.1::8181",
                "--volume", f"{root}:{root}", "--volume", f"{workspace}:{workspace}",
                "--env", f"CATALOG_WAREHOUSE={root}/warehouse",
                "--env", f"CATALOG_URI=jdbc:sqlite:{root}/catalog.sqlite", IMAGE,
            ], check=True, stdout=subprocess.DEVNULL)
            port = subprocess.check_output(["docker", "port", name, "8181/tcp"], text=True).strip().rsplit(":", 1)[1]
            uri = "http://127.0.0.1:" + port
            deadline = time.monotonic() + 45
            while True:
                try:
                    with urlopen(uri + "/v1/config", timeout=2) as response:
                        json.load(response)
                    break
                except (OSError, HTTPException):
                    if time.monotonic() >= deadline:
                        subprocess.run(["docker", "logs", name], check=False)
                        raise RuntimeError("local Iceberg catalog did not become ready")
                    time.sleep(0.2)
            print(f"Local Iceberg catalog: {uri}; workspace: {workspace}", flush=True)
            return subprocess.call(command, env={**os.environ, "DOCSPEC_ICEBERG_URI": uri, "TMPDIR": str(root)})
        finally:
            with suppress(OSError):
                subprocess.run(["docker", "rm", "--force", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    raise SystemExit(main())
