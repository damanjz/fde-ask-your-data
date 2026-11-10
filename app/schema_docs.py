"""Load warehouse/schema_docs.yaml: what the agent reads, and the source of the table allowlist."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

DOCS_PATH = Path(__file__).resolve().parent.parent / "warehouse" / "schema_docs.yaml"


@dataclass
class Column:
    name: str
    type: str
    doc: str
    values: list[str] | None = None
    sample: bool = False


@dataclass
class Table:
    name: str
    description: str
    columns: list[Column] = field(default_factory=list)


@dataclass
class SchemaDocs:
    as_of: str
    today: str
    conventions: list[str]
    relationships: list[str]
    tables: dict[str, Table]
    examples: list[dict]


def _column(name: str, spec) -> Column:
    if isinstance(spec, str):
        typ, _, doc = spec.partition("|")
        return Column(name, typ.strip(), doc.strip())
    return Column(name, str(spec["type"]), str(spec["doc"]), spec.get("values"), bool(spec.get("sample")))


def parse(raw: dict) -> SchemaDocs:
    tables = {
        tname: Table(tname, t["description"], [_column(c, s) for c, s in t["columns"].items()])
        for tname, t in raw["tables"].items()
    }
    return SchemaDocs(
        as_of=str(raw["as_of"]),
        today=str(raw["today"]),
        conventions=list(raw["conventions"]),
        relationships=list(raw["relationships"]),
        tables=tables,
        examples=list(raw.get("examples", [])),
    )


@lru_cache(maxsize=1)
def load(path: str | None = None) -> SchemaDocs:
    p = Path(path) if path else DOCS_PATH
    return parse(yaml.safe_load(p.read_text(encoding="utf-8")))


SCHEMA_SQL_PATH = Path(__file__).resolve().parent.parent / "warehouse" / "schema.sql"


def ddl_for_allowed_tables(docs: SchemaDocs, path: Path | None = None) -> str:
    """Raw CREATE TABLE / CREATE VIEW text for the allowed tables only, as written in schema.sql."""
    text = (path or SCHEMA_SQL_PATH).read_text(encoding="utf-8")
    blocks = []
    for m in re.finditer(r"CREATE TABLE (\w+) \(.*?\n\);", text, re.DOTALL):
        if m.group(1) in docs.tables:
            blocks.append(m.group(0))
    for m in re.finditer(r"CREATE VIEW (\w+) AS.*?;", text, re.DOTALL):
        if m.group(1) in docs.tables:
            blocks.append(m.group(0))
    return "\n\n".join(blocks)
