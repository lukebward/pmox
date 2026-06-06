import pytest

from pmox.safety import (
    DESTRUCTIVE_OPS,
    ConfirmationRequired,
    DangerousNotEnabled,
    confirm,
    is_destructive,
    require_dangerous,
)


def test_assume_yes_proceeds():
    assert confirm("delete vm 100", assume_yes=True) is True


def test_non_interactive_without_yes_raises():
    with pytest.raises(ConfirmationRequired):
        confirm("delete vm 100", assume_yes=False, interactive=False)


def test_interactive_yes():
    assert confirm("delete vm 100", interactive=True, prompt_func=lambda _m: True) is True


def test_interactive_no():
    assert confirm("delete vm 100", interactive=True, prompt_func=lambda _m: False) is False


def test_is_destructive():
    assert is_destructive("delete")
    assert is_destructive("stop")
    assert is_destructive("migrate")
    assert not is_destructive("start")
    assert {"rollback", "reset"} <= DESTRUCTIVE_OPS


def test_require_dangerous_blocks_when_disabled():
    with pytest.raises(DangerousNotEnabled):
        require_dangerous(False)


def test_require_dangerous_allows_when_enabled():
    require_dangerous(True)  # must not raise


def test_set_requires_confirmation():
    from pmox.safety import set_requires_confirmation
    assert set_requires_confirmation({"cores": "4"}) is False
    assert set_requires_confirmation({"delete": "net1"}) is True
