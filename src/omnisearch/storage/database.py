from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.event import listen
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, url: str) -> None:
        sqlite = url.startswith("sqlite")
        connect_args = (
            {"check_same_thread": False, "timeout": 30} if sqlite else {}
        )
        self.engine: Engine = create_engine(url, connect_args=connect_args)
        if sqlite:
            listen(self.engine, "connect", self._configure_sqlite)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    @staticmethod
    def _configure_sqlite(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    def create_schema(self) -> None:
        from omnisearch.storage import tables  # noqa: F401

        Base.metadata.create_all(self.engine)

    def drop_schema(self) -> None:
        from omnisearch.storage import tables  # noqa: F401

        Base.metadata.drop_all(self.engine)
