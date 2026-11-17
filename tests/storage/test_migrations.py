from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from omnisearch.config import get_settings
from omnisearch.storage import tables  # noqa: F401
from omnisearch.storage.database import Base


def test_upgrade_reconciles_legacy_document_versions(
    tmp_path: Path, monkeypatch
) -> None:
    database_path = tmp_path / "legacy.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE document_versions DROP COLUMN object_uri"))
        connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        connection.execute(
            text("INSERT INTO alembic_version(version_num) VALUES ('0001_initial')")
        )
    engine.dispose()

    monkeypatch.setenv("OMNISEARCH_DATABASE_URL", database_url)
    get_settings.cache_clear()
    project_root = Path(__file__).resolve().parents[2]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    command.upgrade(config, "head")

    upgraded = create_engine(database_url)
    columns = {column["name"] for column in inspect(upgraded).get_columns("document_versions")}
    assert "object_uri" in columns
    upgraded.dispose()
    get_settings.cache_clear()
