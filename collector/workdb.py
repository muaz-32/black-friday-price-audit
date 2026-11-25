"""A DuckDB working database that can spill to disk.

The state updates (collector.products, collector.variants) hold tens of
millions of rows. In an in-memory database those tables cannot leave memory:
measured on 2026-11-25 with full-size inputs, the product update of a
full-catalog snapshot peaked at 24 GB, on GitHub runners with 16 GB, and twice
(22 and 24 Nov) the runner stopped answering and the job was lost. A database
file next to the state, with a memory cap, spills to disk instead.

    with workdb.connect(args.state) as db:
        ...
"""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

import duckdb

MEMORY = os.environ.get("BF_DUCKDB_MEMORY", "6GB")


@contextmanager
def connect(near: Path):
    """A fresh database in a temporary folder beside `near`, removed afterwards."""
    folder = Path(tempfile.mkdtemp(prefix=".workdb-", dir=near))
    db = duckdb.connect(str(folder / "work.duckdb"))
    try:
        db.execute(f"SET memory_limit = '{MEMORY}'")
        db.execute(f"SET temp_directory = '{folder / 'spill'}'")
        db.execute("SET preserve_insertion_order = false")  # every output is written with ORDER BY
        yield db
    finally:
        db.close()
        shutil.rmtree(folder, ignore_errors=True)
