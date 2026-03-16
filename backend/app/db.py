from __future__ import annotations

from typing import Any

from sqlalchemy import Boolean, Float, Integer, String, Text, create_engine, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import declarative_base, sessionmaker

from .core.settings import DATABASE_URL


connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    future=True,
    pool_pre_ping=not DATABASE_URL.startswith("sqlite"),
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


def init_db() -> None:
    from . import models  # noqa: F401

    try:
        Base.metadata.create_all(bind=engine)
        _ensure_missing_columns()
    except OperationalError as exc:
        raise RuntimeError(
            "无法连接 PostgreSQL，请确认 `ADMIN_PANEL_DATABASE_URL`（或 ADMIN_PANEL_PG_* 环境变量）已正确配置，且目标数据库已提前创建。"
        ) from exc


def _ensure_missing_columns() -> None:
    inspector = inspect(engine)
    try:
        table_names = set(inspector.get_table_names())
    except Exception:
        return

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in table_names:
                continue
            try:
                existing_columns = {item["name"] for item in inspector.get_columns(table.name)}
            except Exception:
                continue
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                ddl = _build_add_column_ddl(table.name, column)
                if ddl:
                    conn.execute(text(ddl))


def _build_add_column_ddl(table_name: str, column: Any) -> str:
    column_type = column.type.compile(dialect=engine.dialect)
    parts = [f'ALTER TABLE "{table_name}" ADD COLUMN "{column.name}" {column_type}']
    default_sql = _column_default_sql(column)
    if default_sql:
        parts.append(f"DEFAULT {default_sql}")
    if not column.nullable:
        parts.append("NOT NULL")
    return " ".join(parts)


def _column_default_sql(column: Any) -> str:
    default = getattr(column, "default", None)
    if default is not None and getattr(default, "is_scalar", False):
        return _sql_literal(default.arg)
    if column.nullable:
        return ""
    python_type = getattr(column.type, "python_type", None)
    if python_type is bool or isinstance(column.type, Boolean):
        return "1" if DATABASE_URL.startswith("sqlite") else "false"
    if python_type is int or isinstance(column.type, Integer):
        return "0"
    if python_type is float or isinstance(column.type, Float):
        return "0"
    if python_type is str or isinstance(column.type, (String, Text)):
        return _sql_literal("")
    return "''"


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"
