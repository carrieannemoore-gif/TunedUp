import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.brain import BrainResult, parse_decisions  # noqa: E402
from agent.config import load_config  # noqa: E402

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "config.yaml").write_text((ROOT / "config.yaml").read_text())
    return load_config(env={"TRADING_MODE": "paper"}, root=tmp_path)


class FakeBrain:
    """Returns a canned JSON response, exercising the same parser as the real brain."""

    def __init__(self, payload: str):
        self.payload = payload
        self.contexts = []

    def decide(self, context) -> BrainResult:
        self.contexts.append(context)
        return parse_decisions(self.payload)
