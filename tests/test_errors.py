"""The typed-error hierarchy that feeds structured fields into the JSON envelope."""

from pmox.errors import NotFoundError, PlanError, PmoxError, TaskFailed, TaskTimeout


def test_pmox_error_carries_extra():
    e = PmoxError("boom", extra={"hint": "do X", "vmid": 100})
    assert str(e) == "boom"
    assert e.extra == {"hint": "do X", "vmid": 100}


def test_pmox_error_extra_defaults_to_empty_dict():
    assert PmoxError("boom").extra == {}


def test_pmox_error_copies_extra():
    src = {"hint": "do X"}
    e = PmoxError("boom", extra=src)
    src["hint"] = "mutated"
    assert e.extra == {"hint": "do X"}


def test_task_timeout_is_both_pmox_and_timeout_error():
    assert issubclass(TaskTimeout, PmoxError)
    assert issubclass(TaskTimeout, TimeoutError)


def test_task_failed_is_both_pmox_and_runtime_error():
    assert issubclass(TaskFailed, PmoxError)
    assert issubclass(TaskFailed, RuntimeError)


def test_plan_error_is_both_pmox_and_runtime_error():
    assert issubclass(PlanError, PmoxError)
    assert issubclass(PlanError, RuntimeError)


def test_not_found_error_is_pmox_and_lookup_error():
    err = NotFoundError("nope", extra={"hint": "re-list"})
    assert isinstance(err, PmoxError)
    assert isinstance(err, LookupError)
    assert err.extra == {"hint": "re-list"}
