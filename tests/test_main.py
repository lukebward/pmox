def test_main_module_exposes_callable_main():
    import pmox.__main__ as entry

    assert callable(entry.main)
