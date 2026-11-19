"""SQL engines for the verifier. DuckDB is the primary engine; stdlib sqlite3 is the fallback.

Both run the same files from sql/verifier/. The only engine-specific primitive is
convert_half_up(src_minor, rate_text, src_exp, dst_exp), the exact conversion of SPEC §4:
    ROUND_HALF_UP(src_minor / 10^src_exp * rate * 10^dst_exp)
computed as an integer ratio N / D with half-up as floor((2N + D) / 2D) for N >= 0.
DuckDB evaluates it as a SQL macro over HUGEINT; SQLite cannot hold the intermediate product
in 64 bits, so it gets a Python integer function of the same formula. Neither uses floats.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sqlite3

from .loader import TABLE_DDL

SQL_DIR = Path(__file__).resolve().parents[2] / "sql" / "verifier"
ENGINES = ("duckdb", "sqlite")

DUCKDB_PRIMITIVES = """
CREATE MACRO pow10(n) AS CAST('1' || repeat('0', CAST(n AS INTEGER)) AS HUGEINT);
CREATE MACRO rate_scale(r) AS CASE WHEN strpos(r, '.') > 0 THEN length(r) - strpos(r, '.') ELSE 0 END;
CREATE MACRO rate_units(r) AS CAST(replace(r, '.', '') AS HUGEINT);
CREATE MACRO convert_half_up(src, r, se, de) AS
  CASE WHEN src < 0 THEN NULL ELSE
  CAST((2 * CAST(src AS HUGEINT) * rate_units(r) * pow10(de) + pow10(se + rate_scale(r)))
       // (2 * pow10(se + rate_scale(r))) AS BIGINT) END;
"""


def _sqlite_convert_half_up(src, rate, src_exp, dst_exp):
    if src is None or rate is None or src_exp is None or dst_exp is None or src < 0:
        return None
    whole, _, fraction = rate.partition(".")
    numerator = 2 * src * int(whole + fraction) * 10 ** dst_exp
    denominator = 10 ** (src_exp + len(fraction))
    return (numerator + denominator) // (2 * denominator)


def default_engine() -> str:
    requested = os.environ.get("AUDITLAB_VERIFIER_ENGINE")
    if requested:
        if requested not in ENGINES:
            raise ValueError(f"AUDITLAB_VERIFIER_ENGINE must be one of {ENGINES}")
        return requested
    return "duckdb" if importlib.util.find_spec("duckdb") is not None else "sqlite"


def statements(text: str):
    """Split on semicolons outside single-quoted literals; drop full-line comments."""
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("--")]
    current, quoted = [], False
    for char in "\n".join(lines):
        if char == "'":
            quoted = not quoted
        if char == ";" and not quoted:
            if "".join(current).strip():
                yield "".join(current).strip()
            current = []
        else:
            current.append(char)
    if "".join(current).strip():
        yield "".join(current).strip()


def sql_files():
    files = sorted(SQL_DIR.glob("*.sql"))
    if not files:
        raise FileNotFoundError(f"no verifier SQL in {SQL_DIR}")
    return files


class Database:
    def __init__(self, engine: str):
        self.engine = engine
        if engine == "duckdb":
            import duckdb
            self.version = duckdb.__version__
            self.connection = duckdb.connect(":memory:")
            for statement in statements(DUCKDB_PRIMITIVES):
                self.connection.execute(statement)
        elif engine == "sqlite":
            self.version = sqlite3.sqlite_version
            self.connection = sqlite3.connect(":memory:")
            self.connection.create_function("convert_half_up", 4, _sqlite_convert_half_up,
                                            deterministic=True)
        else:
            raise ValueError(f"unknown engine {engine!r}")

    def load(self, tables: dict[str, list[tuple]]):
        for name, columns in TABLE_DDL.items():
            self.connection.execute(f"CREATE TABLE {name} ({columns})")
            rows = tables.get(name, [])
            if rows:
                marks = ", ".join("?" for _ in rows[0])
                self.connection.executemany(f"INSERT INTO {name} VALUES ({marks})", rows)

    def run_rules(self):
        for path in sql_files():
            for statement in statements(path.read_text(encoding="utf-8")):
                self.connection.execute(statement)

    def query(self, sql: str) -> list[tuple]:
        return self.connection.execute(sql).fetchall()

    def close(self):
        self.connection.close()
