"""Group C tests -- the S7 trace grammar (:mod:`chainaim_settlement_core.trace`).

Asserts each emitter produces the exact frozen line shape and that lines split on ``:``
unambiguously (every embedded value is colon-free), so the adversarial validators can
reconstruct billed-vs-verified from the trace.
"""

from chainaim_settlement_core import trace

REF = "a" * 64  # a colon-free, hex-like ref
CHK = "b" * 64


def test_stream_open_grammar():
    assert trace.stream_open(REF, "buyer", "seller", 100, 500, 0) == (
        f"stream-open:{REF}:buyer:seller:100:500:0"
    )


def test_tick_grammar():
    assert trace.tick(REF, 0, 100, 1) == f"tick:{REF}:0:100:1"


def test_ack_l1_grammar():
    assert trace.ack_l1(REF, 3) == f"ack:{REF}:3"


def test_ack_content_grammar():
    assert trace.ack_content(REF, 0, "00ff", CHK) == f"ack:{REF}:0:00ff:{CHK}"


def test_ack_content_null_checksum_leaves_trailing_empty_field():
    line = trace.ack_content(REF, 0, "00ff", None)
    assert line == f"ack:{REF}:0:00ff:"
    assert line.endswith(":")


def test_gate_pass_grammar():
    assert trace.gate(REF, 0, True) == f"gate:{REF}:0:pass"


def test_gate_fail_grammar():
    assert trace.gate(REF, 1, False) == f"gate:{REF}:1:fail"


def test_stream_close_grammar():
    assert (
        trace.stream_close(REF, 2, 300, 3, "closed")
        == f"stream-close:{REF}:2:300:3:closed"
    )


def test_lines_split_on_colon_unambiguously():
    assert trace.stream_open(REF, "buyer", "seller", 100, 500, 0).split(":") == [
        "stream-open",
        REF,
        "buyer",
        "seller",
        "100",
        "500",
        "0",
    ]
    assert trace.tick(REF, 0, 100, 1).split(":") == ["tick", REF, "0", "100", "1"]
    assert trace.stream_close(REF, 2, 300, 3, "checksum-mismatch").split(":") == [
        "stream-close",
        REF,
        "2",
        "300",
        "3",
        "checksum-mismatch",
    ]
