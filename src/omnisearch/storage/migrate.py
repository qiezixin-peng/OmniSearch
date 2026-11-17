from pathlib import Path

from alembic import command
from alembic.config import Config


def run() -> None:
    project_root = Path(__file__).resolve().parents[3]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    command.upgrade(config, "head")


if __name__ == "__main__":
    run()
