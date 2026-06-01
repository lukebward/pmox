import io
import json

import pytest
from rich.console import Console

from pmox.output import (
    Column,
    build_kv_table,
    build_table,
    emit,
    fmt_epoch,
    human_bytes,
    human_uptime,
    percent,
    status_fmt,
)


def render(renderable):
    buf = io.StringIO()
    Console(file=buf, width=200, color_system=None).print(renderable)
    return buf.getvalue()


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "-"),
        ("", "-"),
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.0 KiB"),
        (1048576, "1.0 MiB"),
        (1073741824, "1.0 GiB"),
        (1610612736, "1.5 GiB"),
    ],
)
def test_human_bytes(value, expected):
    assert human_bytes(value) == expected


def test_human_uptime():
    assert human_uptime(0) == "-"
    assert human_uptime(59) == "0m"
    assert human_uptime(60) == "1m"
    assert human_uptime(3600) == "1h"  # zero-minute component is omitted
    assert human_uptime(3660) == "1h 1m"
    assert human_uptime(90061) == "1d 1h 1m"


def test_percent():
    assert percent(None) == "-"
    assert percent(0.1) == "10.0%"
    assert percent(5, 10) == "50.0%"
    assert percent(1, 0) == "-"


def test_fmt_epoch_handles_garbage():
    assert fmt_epoch(0) == "-"
    assert fmt_epoch("not-a-number") == "not-a-number"


def test_status_fmt_text_survives_render():
    assert "running" in render(status_fmt("running"))


def test_build_table_renders_values():
    rows = [{"node": "pve1", "status": "running", "mem": 1073741824}]
    cols = [
        Column("Node", "node"),
        Column("Status", "status", status_fmt),
        Column("Mem", "mem", human_bytes),
    ]
    out = render(build_table(rows, cols, title="Nodes"))
    assert "pve1" in out
    assert "running" in out
    assert "1.0 GiB" in out


def test_build_table_row_formatter():
    rows = [{"disk": 5, "maxdisk": 10}]
    cols = [Column("Use%", row_formatter=lambda r: percent(r.get("disk"), r.get("maxdisk")))]
    assert "50.0%" in render(build_table(rows, cols))


def test_build_kv_table():
    out = render(build_kv_table({"name": "web", "nested": {"x": 2}}))
    assert "name" in out and "web" in out
    assert "nested" in out


def test_emit_json(capsys):
    emit([{"vmid": 100}], json_output=True)
    captured = capsys.readouterr().out
    assert json.loads(captured) == [{"vmid": 100}]


def test_emit_empty_list_shows_message():
    buf = io.StringIO()
    emit([], console_=Console(file=buf, width=80, color_system=None))
    assert "no results" in buf.getvalue()


def test_emit_dict_renders_kv_table():
    buf = io.StringIO()
    emit({"status": "running"}, console_=Console(file=buf, width=80, color_system=None))
    out = buf.getvalue()
    assert "status" in out and "running" in out


def test_human_bytes_non_numeric():
    assert human_bytes("abc") == "abc"


def test_human_bytes_negative():
    assert human_bytes(-2048) == "-2.0 KiB"


def test_human_uptime_non_numeric():
    assert human_uptime("abc") == "abc"


def test_percent_non_numeric():
    assert percent("abc") == "-"


def test_status_fmt_none():
    assert status_fmt(None) == "-"


def test_cell_getattr_on_object():
    class Row:
        node = "pve9"

    assert "pve9" in render(build_table([Row()], [Column("Node", "node")]))


def test_cell_row_formatter_exception_returns_dash():
    def boom(_row):
        raise ValueError("kaboom")

    assert "-" in render(build_table([{"a": 1}], [Column("X", row_formatter=boom)]))


def test_emit_list_of_dicts_without_columns():
    buf = io.StringIO()
    emit([{"a": 1, "b": 2}], console_=Console(file=buf, width=120, color_system=None))
    out = buf.getvalue()
    assert "a" in out and "b" in out


def test_emit_list_of_scalars():
    buf = io.StringIO()
    emit([1, 2, 3], console_=Console(file=buf, width=80, color_system=None))
    out = buf.getvalue()
    assert "1" in out and "3" in out


def test_emit_scalar():
    buf = io.StringIO()
    emit("hello", console_=Console(file=buf, width=80, color_system=None))
    assert "hello" in buf.getvalue()
