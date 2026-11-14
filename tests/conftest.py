import pytest

import config


@pytest.fixture(autouse=True)
def both_strategies_enabled(monkeypatch):
    """Tests describe the bot with both strategies on, whatever config.py says today."""
    monkeypatch.setattr(config, "SPREAD_ENABLED", ("comb", "nectar"), raising=False)
