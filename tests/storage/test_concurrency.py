from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text

from omnisearch.core.research import ResearchCoordinator
from omnisearch.domain.models import ResearchCreate
from omnisearch.storage.database import Database
from omnisearch.storage.repository import SqlResearchRepository


def test_sqlite_supports_parallel_agent_task_creation(tmp_path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'parallel.db').as_posix()}")
    database.create_schema()
    coordinator = ResearchCoordinator(SqlResearchRepository(database))

    def create(index: int):
        return coordinator.create(ResearchCreate(question=f"parallel research task {index}"))

    with ThreadPoolExecutor(max_workers=12) as pool:
        tasks = list(pool.map(create, range(40)))

    assert len({task.id for task in tasks}) == 40
    assert all(coordinator.get(task.id).question.startswith("parallel") for task in tasks)
    with database.engine.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar_one().lower() == "wal"
