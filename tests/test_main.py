import pytest

import pmox.cli as cli


def test_main_module_exposes_callable_main():
    import pmox.__main__ as entry

    assert callable(entry.main)


# ------------------------------------------------------------------ _force_utf8 --


def test_main_reconfigures_streams_to_utf8(monkeypatch):
    calls = []

    class _Stream:
        def reconfigure(self, **kwargs):
            calls.append(kwargs)

        def write(self, *args, **kwargs):
            pass

        def flush(self):
            pass

    monkeypatch.setattr(cli.sys, "stdout", _Stream())
    monkeypatch.setattr(cli.sys, "stderr", _Stream())
    monkeypatch.setattr(cli.sys, "argv", ["pmox", "--version"])
    with pytest.raises(SystemExit):
        cli.main()
    assert {"encoding": "utf-8", "errors": "replace"} in calls


def test_force_utf8_noop_when_stream_lacks_reconfigure():
    class _Stream:
        pass

    cli._force_utf8(_Stream())  # must not raise


def test_force_utf8_swallows_reconfigure_errors():
    class _Stream:
        def reconfigure(self, **kwargs):
            raise RuntimeError("boom")

    cli._force_utf8(_Stream())  # must not raise
