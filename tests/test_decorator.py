"""Tests for the @petri_net decorator."""

import pytest
from petritype import petri_net


def test_decorator_attaches_config():
    """Decorator should attach _petri_net_config to function."""

    @petri_net(name="test-net", mode="manual")
    def my_net():
        return "graph"

    assert hasattr(my_net, "_petri_net_config")
    config = my_net._petri_net_config
    assert config["name"] == "test-net"
    assert config["mode"] == "manual"
    assert config["function"] == "my_net"


def test_decorator_preserves_function_behavior():
    """Decorated function should still work normally."""

    @petri_net(name="test-net")
    def my_net():
        return {"places": [], "transitions": []}

    result = my_net()
    assert result == {"places": [], "transitions": []}


def test_decorator_with_all_options():
    """Decorator should accept all optional parameters."""

    @petri_net(
        name="scheduled-net",
        mode="cron",
        description="Runs every hour",
        schedule="0 * * * *",
    )
    def scheduled_net():
        return "graph"

    config = scheduled_net._petri_net_config
    assert config["name"] == "scheduled-net"
    assert config["mode"] == "cron"
    assert config["description"] == "Runs every hour"
    assert config["schedule"] == "0 * * * *"


def test_cron_mode_requires_schedule():
    """mode='cron' without schedule should raise ValueError."""
    with pytest.raises(ValueError, match="requires a schedule"):

        @petri_net(name="bad-cron", mode="cron")
        def bad_cron():
            pass


def test_schedule_requires_cron_mode():
    """schedule without mode='cron' should raise ValueError."""
    with pytest.raises(ValueError, match="only valid for mode='cron'"):

        @petri_net(name="bad-schedule", mode="manual", schedule="0 * * * *")
        def bad_schedule():
            pass


def test_decorator_preserves_function_name():
    """Decorated function should keep its original name."""

    @petri_net(name="test-net")
    def my_special_net():
        pass

    assert my_special_net.__name__ == "my_special_net"


def test_default_mode_is_manual():
    """Default mode should be 'manual'."""

    @petri_net(name="default-mode")
    def default_net():
        pass

    assert default_net._petri_net_config["mode"] == "manual"
