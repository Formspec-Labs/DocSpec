"""Bounded read-only check of selected tools against DocSpec codec rules.
Run from the DocSpec checkout with PYTHONPATH=src .venv/bin/python docs/history/probes/2026-09-13-core-codec-probe.py
No DocSpec repositories or production data are opened or mutated.
"""
import hashlib
import importlib.metadata
import json
import re
import duckdb
import msgspec
from docspec.domain.identity import canonical_json_bytes

con = duckdb.connect()
results = {"versions": {n: importlib.metadata.version(n) for n in ("duckdb", "msgspec", "pyarrow", "jsonschema-rs")}}
values = [None, True, False, 0, 9007199254740991, -9007199254740991, "1", [1,"1",None],
          "\x1f", {"a":"\x1f"}, {"\ue000":1,"\U00010000":2}]
checks=[]
for value in values:
    parent = canonical_json_bytes({"v": value}).decode()
    expected = canonical_json_bytes(value)
    extracted = con.execute("SELECT json_extract(?, '/v')::VARCHAR", [parent]).fetchone()[0].encode()
    checks.append({"value":value,"expected":expected.decode(),"extracted":extracted.decode(),
                   "equal_bytes":expected==extracted,
                   "equal_sha256":hashlib.sha256(expected).digest()==hashlib.sha256(extracted).digest()})
results["canonical_extraction"] = checks
results["presence"] = [{"parent":p,"value_and_type":con.execute(
    "SELECT json_extract(?, '/a'), json_type(?, '/a')", [p,p]).fetchone()} for p in ('{"a":null}','{}','{"a":1}','{"a":"1"}')]
results["invalid_pointer"]={"pointer":"/x~2","duckdb_result":con.execute(
    "SELECT json_extract(?, ?)", ['{"x":1}', '/x~2']).fetchone()[0],
    "valid_rfc6901_syntax":re.fullmatch(r'(?:/(?:[^~/]|~[01])*)*','/x~2') is not None}
results["duplicate_keys"]={"input":'{"a":1,"a":2}',"msgspec_decoded":msgspec.json.decode(b'{"a":1,"a":2}')}
results["patch_functions"]=[x[0] for x in con.execute(
    "SELECT function_name FROM duckdb_functions() WHERE function_name ILIKE '%patch%' ORDER BY function_name").fetchall()]
print(json.dumps(results,indent=2,ensure_ascii=True))
