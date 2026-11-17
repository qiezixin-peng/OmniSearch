from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, url: str) -> None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine: Engine = create_engine(url, connect_args=connect_args)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def create_schema(self) -> None:
        from omnisearch.storage import tables  # noqa: F401

        Base.metadata.create_all(self.engine)

    def drop_schema(self) -> None:
        from omnisearch.storage import tables  # noqa: F401

        Base.metadata.drop_all(self.engine)
