from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import event
import config as _config
from config import DATABASE_URL as _INITIAL_DATABASE_URL
import logging
import os

logger = logging.getLogger(__name__)

def _get_current_database_url() -> str:
    """Return current DATABASE_URL, recomputing from STORAGE_DIR if config was patched by tests."""
    try:
        current = getattr(_config, "DATABASE_URL", _INITIAL_DATABASE_URL)
        # If DATABASE_URL was explicitly changed after import, respect it
        if current != _INITIAL_DATABASE_URL:
            return current
        storage = getattr(_config, "STORAGE_DIR", None)
        if storage:
            expected = f"sqlite+aiosqlite:///{os.path.join(storage, 'app.db')}"
            if expected != _INITIAL_DATABASE_URL:
                return expected
        return current
    except Exception:
        return _INITIAL_DATABASE_URL

# Expose current DATABASE_URL for importers (kept in sync with _get_current_database_url)
DATABASE_URL = _get_current_database_url()

def _create_engine(url: str):
    return create_async_engine(
        url,
        echo=False,
        pool_size=3,
        max_overflow=5,
        pool_timeout=30,
        pool_recycle=3600,
        pool_pre_ping=True,
        connect_args={"timeout": 30},
    )

engine = _create_engine(DATABASE_URL)

def _apply_pragma_to_engine(eng):
    @event.listens_for(eng.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

_apply_pragma_to_engine(engine)

# Keep the actual factory in a mutable cell so imported references can stay valid after reset
_async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

class _AsyncSessionProxy:
    """Proxy that always delegates to the current factory, handling late STORAGE_DIR patch."""
    def __call__(self, *args, **kwargs):
        # Auto-reset if config changed after import (handles direct async_session() usage)
        if _get_current_database_url() != DATABASE_URL:
            reset_engine()
        return _async_session_factory(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(_async_session_factory, name)

async_session = _AsyncSessionProxy()  # type: ignore

def reset_engine(new_url: str | None = None):
    """Recreate engine and async_session from current config. Used for test isolation when STORAGE_DIR changes after import."""
    global engine, _async_session_factory, DATABASE_URL
    url = new_url or _get_current_database_url()
    # Also sync config.DATABASE_URL so other importers see updated value
    try:
        _config.DATABASE_URL = url
    except Exception:
        pass
    DATABASE_URL = url
    new_engine = _create_engine(url)
    _apply_pragma_to_engine(new_engine)
    engine = new_engine
    _async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    # Auto-reset if config was patched after import (test isolation)
    if _get_current_database_url() != DATABASE_URL:
        reset_engine()
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()


def _get_existing_columns_sync(conn, table_name: str) -> set[str]:
    """同步辅助：读取 SQLite PRAGMA table_info 返回已有列名集合。"""
    result = conn.exec_driver_sql(f"PRAGMA table_info({table_name})")
    return {row[1] for row in result}


def _table_exists_sync(conn, table_name: str) -> bool:
    """同步辅助：检查表是否存在。"""
    result = conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    return result.fetchone() is not None


def _migrate_table_sync(conn, table_name: str, columns: list[tuple[str, str]]) -> dict:
    """同步辅助：为单张表追加缺失列，返回每列的迁移结果。"""
    results = {}
    if not _table_exists_sync(conn, table_name):
        results["__table__"] = "skipped: table does not exist"
        return results

    existing = _get_existing_columns_sync(conn, table_name)
    results["__existing_columns__"] = list(existing)

    for col, typ in columns:
        if col in existing:
            results[col] = "already exists"
            continue
        try:
            # 列名可能是 SQLite 保留关键字（如 references），必须用双引号包裹
            conn.exec_driver_sql(f'ALTER TABLE {table_name} ADD COLUMN "{col}" {typ}')
            results[col] = "added"
        except Exception as e:
            results[col] = f"failed: {e}"
            logger.error("Failed to add column %s.%s: %s", table_name, col, e)
    return results


def _ensure_migrations_succeeded(results_by_table: dict[str, dict]) -> None:
    failures = []
    for table_name, results in results_by_table.items():
        for column_name, result in results.items():
            if isinstance(result, str) and result.startswith("failed:"):
                failures.append(f"{table_name}.{column_name}: {result}")
    if failures:
        raise RuntimeError("Database migration failed: " + "; ".join(failures))


def _repair_legacy_references_sync(conn) -> dict[str, int]:
    """只修复可无损保留历史记录的旧库孤立引用。"""
    repairs: dict[str, int] = {}
    statements = {
        "processing_tasks.question_id": (
            "UPDATE processing_tasks SET question_id=NULL "
            "WHERE question_id IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM questions WHERE questions.id=processing_tasks.question_id)"
        ),
        "corrections.question_id": (
            "UPDATE corrections SET question_id=NULL "
            "WHERE question_id IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM questions WHERE questions.id=corrections.question_id)"
        ),
        "corrections.paper_id": (
            "UPDATE corrections SET paper_id=NULL "
            "WHERE paper_id IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM papers WHERE papers.id=corrections.paper_id)"
        ),
        "corrections.matched_question_id": (
            "UPDATE corrections SET matched_question_id='' "
            "WHERE matched_question_id IS NOT NULL AND matched_question_id<>'' AND NOT EXISTS "
            "(SELECT 1 FROM questions WHERE questions.id=corrections.matched_question_id)"
        ),
    }
    for label, statement in statements.items():
        result = conn.exec_driver_sql(statement)
        repairs[label] = max(int(result.rowcount or 0), 0)
    return repairs


def _assert_foreign_keys_clean_sync(conn) -> None:
    violations = list(conn.exec_driver_sql("PRAGMA foreign_key_check"))
    if violations:
        preview = ", ".join(str(tuple(row)) for row in violations[:10])
        raise RuntimeError(f"Database foreign key check failed: {preview}")


async def init_db():
    if _get_current_database_url() != DATABASE_URL:
        reset_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Auto-migrate missing columns — questions table
        questions_cols = [
            ("bank", "VARCHAR DEFAULT 'default'"),
            ("standard_answer", "TEXT DEFAULT ''"),
            ("question_type", "VARCHAR DEFAULT ''"),
            ("score_points_html", "TEXT DEFAULT ''"),
            ("diagram_places", "TEXT DEFAULT '[]'"),
            ("diagram_description", "TEXT DEFAULT ''"),
            ("region", "VARCHAR DEFAULT ''"),
            ("avg_score", "FLOAT"),
            ("user_hint", "TEXT DEFAULT ''"),
            ("audit_flags", "TEXT DEFAULT '[]'"),
            ("is_resolved", "INTEGER DEFAULT 0"),
            ("handwriting_notes", "TEXT DEFAULT ''"),
            ("structure_graph", "TEXT DEFAULT NULL"),
            ("structure_graph_info", "TEXT DEFAULT NULL"),
            ("image_roles", "TEXT DEFAULT NULL"),
            ("multi_images", "TEXT DEFAULT NULL"),
            ("comparison_regions", "TEXT DEFAULT NULL"),
            ("reference_svg_path", "VARCHAR DEFAULT ''"),
            ("reference_svg_status", "VARCHAR DEFAULT 'pending'"),
            ("reference_svg_error", "TEXT DEFAULT ''"),
            ("capture_mode", "VARCHAR DEFAULT 'single_question'"),
            ("capture_group_id", "VARCHAR DEFAULT ''"),
            ("capture_index", "INTEGER DEFAULT 0"),
        ]
        questions_result = await conn.run_sync(
            lambda c: _migrate_table_sync(c, "questions", questions_cols)
        )
        logger.info("questions migration result: %s", questions_result)

        # Auto-migrate missing columns — notes table
        notes_cols = [
            ("typical_questions", "TEXT DEFAULT '[]'"),
            ("source_images", "TEXT DEFAULT '[]'"),
            ("diagram_spec", "TEXT DEFAULT NULL"),
            ("is_structured", "INTEGER DEFAULT 0"),
            ("source_type", "VARCHAR DEFAULT 'manual'"),
            ("sort_order", "INTEGER DEFAULT 0"),
            ("auto_generated", "INTEGER DEFAULT 0"),
            ("references", "TEXT DEFAULT NULL"),
        ]
        notes_result = await conn.run_sync(
            lambda c: _migrate_table_sync(c, "notes", notes_cols)
        )
        logger.info("notes migration result: %s", notes_result)

        # Auto-migrate missing columns — papers table
        papers_cols = [
            ("paper_type", "VARCHAR DEFAULT 'custom'"),
            ("prompt_template_id", "VARCHAR DEFAULT ''"),
            ("custom_prompt", "TEXT DEFAULT ''"),
            ("question_order", "TEXT DEFAULT '[]'"),
            ("answer_sheet_html", "TEXT DEFAULT ''"),
            ("paper_pdf_path", "VARCHAR DEFAULT ''"),
            ("paper_word_path", "VARCHAR DEFAULT ''"),
            ("answer_pdf_path", "VARCHAR DEFAULT ''"),
            ("answer_word_path", "VARCHAR DEFAULT ''"),
            ("generation_params", "TEXT DEFAULT '{}'"),
            ("user_score", "FLOAT"),
        ]
        papers_result = await conn.run_sync(
            lambda c: _migrate_table_sync(c, "papers", papers_cols)
        )
        logger.info("papers migration result: %s", papers_result)

        # Auto-migrate missing columns — corrections table (created by Base.metadata.create_all)
        corrections_cols = [
            ("question_id", "VARCHAR"),
            ("paper_id", "VARCHAR"),
            ("student_answer", "TEXT DEFAULT ''"),
            ("matched_question_id", "VARCHAR DEFAULT ''"),
            ("score", "FLOAT"),
            ("max_score", "FLOAT"),
            ("points", "TEXT DEFAULT '[]'"),
            ("feedback", "TEXT DEFAULT ''"),
            ("error_analysis", "TEXT DEFAULT ''"),
            ("suggestions", "TEXT DEFAULT ''"),
            ("raw_image_path", "VARCHAR DEFAULT ''"),
            ("source_type", "VARCHAR DEFAULT 'single'"),
        ]
        corrections_result = await conn.run_sync(
            lambda c: _migrate_table_sync(c, "corrections", corrections_cols)
        )
        logger.info("corrections migration result: %s", corrections_result)

        # Auto-migrate missing columns — saved_configs table
        saved_configs_cols = [
            ("config_type", "VARCHAR DEFAULT 'paper'"),
            ("name", "VARCHAR DEFAULT ''"),
            ("payload", "TEXT DEFAULT '{}'"),
        ]
        saved_configs_result = await conn.run_sync(
            lambda c: _migrate_table_sync(c, "saved_configs", saved_configs_cols)
        )
        logger.info("saved_configs migration result: %s", saved_configs_result)

        # Auto-migrate missing columns — upload_sessions table
        upload_sessions_cols = [
            ("upload_mode", "VARCHAR DEFAULT 'one_per_image'"),
        ]
        upload_sessions_result = await conn.run_sync(
            lambda c: _migrate_table_sync(c, "upload_sessions", upload_sessions_cols)
        )
        logger.info("upload_sessions migration result: %s", upload_sessions_result)

        migration_results = {
            "questions": questions_result,
            "notes": notes_result,
            "papers": papers_result,
            "corrections": corrections_result,
            "saved_configs": saved_configs_result,
            "upload_sessions": upload_sessions_result,
        }
        _ensure_migrations_succeeded(migration_results)

        repairs = await conn.run_sync(_repair_legacy_references_sync)
        if any(repairs.values()):
            logger.warning("Repaired legacy database references: %s", repairs)
        await conn.run_sync(_assert_foreign_keys_clean_sync)
