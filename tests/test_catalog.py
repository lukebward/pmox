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


def test_resolve_image_catalog_name():
    spec = catalog.resolve_image("ubuntu-24.04")
    assert spec["kind"] == "url"
    assert spec["url"].startswith("https://")
    assert spec["filename"].endswith(".qcow2")


def test_resolve_image_url():
    spec = catalog.resolve_image("https://example.com/img/my-cloud.qcow2")
    assert spec == {
        "kind": "url",
        "url": "https://example.com/img/my-cloud.qcow2",
        "filename": "my-cloud.qcow2",
        "checksum": None,
        "algo": None,
    }


def test_resolve_image_volid():
    spec = catalog.resolve_image("local:import/foo.qcow2")
    assert spec == {"kind": "volid", "volid": "local:import/foo.qcow2"}


def test_image_catalog_filenames_are_qcow2():
    for entry in catalog.IMAGE_CATALOG.values():
        assert entry["filename"].endswith(".qcow2")
        assert entry["url"].startswith("https://")
