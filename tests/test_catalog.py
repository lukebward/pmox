import pytest

from pmox import catalog


def test_size_profiles_values():
    assert catalog.SIZE_PROFILES["small"] == {"cores": 1, "memory": 1024}
    assert catalog.SIZE_PROFILES["medium"] == {"cores": 2, "memory": 4096}
    assert catalog.SIZE_PROFILES["large"] == {"cores": 4, "memory": 8192}


def test_size_params_returns_copy():
    p = catalog.size_params("small")
    assert p == {"cores": 1, "memory": 1024}
    p["cores"] = 99
    assert catalog.SIZE_PROFILES["small"]["cores"] == 1  # not mutated


def test_size_params_unknown_raises():
    with pytest.raises(ValueError):
        catalog.size_params("enormous")


def test_pressure_thresholds_are_fractions():
    assert 0 < catalog.CPU_PRESSURE <= 1
    assert 0 < catalog.MEM_PRESSURE <= 1
    assert 0 < catalog.STORAGE_PRESSURE <= 1
