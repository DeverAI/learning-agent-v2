"""数据库自动迁移测试：验证旧表结构能正确追加新增列。"""

import os
import tempfile
from sqlalchemy import create_engine, Column, Integer, String, Text, JSON
from sqlalchemy.orm import DeclarativeBase

from models.database import _migrate_table_sync


class _TestBase(DeclarativeBase):
    pass


class _OldQuestion(_TestBase):
    __tablename__ = "questions"
    id = Column(String, primary_key=True)
    ocr_text = Column(Text, default="")


class _OldNote(_TestBase):
    __tablename__ = "notes"
    id = Column(String, primary_key=True)
    content = Column(Text, default="")


def _run_sync_migration(db_path: str, table_name: str, cols: list):
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    _TestBase.metadata.create_all(engine)
    with engine.connect() as conn:
        result = _migrate_table_sync(conn, table_name, cols)
        conn.commit()
    engine.dispose()
    return result


def test_migrate_questions_columns():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        cols = [
            ("bank", "VARCHAR DEFAULT 'default'"),
            ("image_roles", "TEXT DEFAULT NULL"),
            ("multi_images", "TEXT DEFAULT NULL"),
            ("comparison_regions", "TEXT DEFAULT NULL"),
            ("reference_svg_path", "VARCHAR DEFAULT ''"),
            ("reference_svg_status", "VARCHAR DEFAULT 'pending'"),
            ("reference_svg_error", "TEXT DEFAULT ''"),
        ]
        result = _run_sync_migration(db_path, "questions", cols)
        assert result.get("bank") == "added", result
        assert result.get("image_roles") == "added", result
        assert result.get("multi_images") == "added", result
        assert result.get("comparison_regions") == "added", result
        assert result.get("reference_svg_path") == "added", result
        assert result.get("reference_svg_status") == "added", result
        assert result.get("reference_svg_error") == "added", result

        # 再次迁移应显示已存在
        result2 = _run_sync_migration(db_path, "questions", cols)
        assert result2.get("bank") == "already exists", result2
        assert result2.get("image_roles") == "already exists", result2


def test_migrate_notes_references():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        cols = [
            ("references", "TEXT DEFAULT NULL"),
            ("is_structured", "INTEGER DEFAULT 0"),
        ]
        result = _run_sync_migration(db_path, "notes", cols)
        assert result.get("references") == "added", result
        assert result.get("is_structured") == "added", result


def test_migrate_table_missing():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        engine = create_engine(f"sqlite:///{db_path}", future=True)
        with engine.connect() as conn:
            result = _migrate_table_sync(conn, "nonexistent", [("col", "TEXT")])
            conn.commit()
        engine.dispose()
        assert result.get("__table__") == "skipped: table does not exist"


def test_migrate_reserved_keyword_references():
    """验证 SQLite 保留关键字 references 用双引号包裹后可正常追加。"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        engine = create_engine(f"sqlite:///{db_path}", future=True)
        _OldNote.metadata.create_all(engine)
        with engine.connect() as conn:
            result = _migrate_table_sync(conn, "notes", [("references", "TEXT DEFAULT NULL")])
            conn.commit()
            # 确认列真的存在
            rows = conn.exec_driver_sql("PRAGMA table_info(notes)").fetchall()
            existing = {row[1] for row in rows}
            conn.commit()
        engine.dispose()
        assert "references" in existing
        assert result.get("references") == "added"


if __name__ == "__main__":
    test_migrate_questions_columns()
    test_migrate_notes_references()
    test_migrate_table_missing()
    test_migrate_reserved_keyword_references()
    print("All migration tests passed")
