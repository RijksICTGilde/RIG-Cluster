"""Regression test for the streaming clone CREATE SCHEMA rewrite (PR62 round-2).

The streaming pump replaced an in-memory buffer with chunked reads, after a
prior revision used readline() (which raises on COPY data lines wider than
asyncio's ~64 KiB StreamReader limit -- a silent correctness regression vs the
old shell pipe). The line-anchored CREATE SCHEMA -> CREATE SCHEMA IF NOT
EXISTS rewrite is the part the rewrite put at risk; it is extracted as a pure
function so it can be tested directly, including the chunk-boundary carry and
arbitrarily wide lines.
"""

from opi.connectors.postgres import _rewrite_create_schema


def test_rewrites_anchored_create_schema() -> None:
    src = b"CREATE SCHEMA myschema;\nSET x = 1;\n"
    assert _rewrite_create_schema(src) == b"CREATE SCHEMA IF NOT EXISTS myschema;\nSET x = 1;\n"


def test_does_not_rewrite_mid_line_occurrence() -> None:
    # Anchored at line start only -- a CREATE SCHEMA inside data/comment stays.
    src = b"-- note: CREATE SCHEMA foo;\nINSERT INTO t VALUES ('CREATE SCHEMA x');\n"
    assert _rewrite_create_schema(src) == src


def test_prefix_swap_is_purely_token_level() -> None:
    # pg_dump -n only ever emits `CREATE SCHEMA <name>;`, never the
    # IF NOT EXISTS form, so this input does not occur in practice. The test
    # pins the deterministic, purely-token-level behaviour: only the leading
    # `CREATE SCHEMA ` is swapped, the rest of the line is untouched.
    src = b"CREATE SCHEMA IF NOT EXISTS s;\n"
    assert _rewrite_create_schema(src) == b"CREATE SCHEMA IF NOT EXISTS IF NOT EXISTS s;\n"


def test_handles_line_far_wider_than_64kib() -> None:
    # The bug class: a single COPY data line many times the 64 KiB readline
    # ceiling must pass through untouched (no rewrite, no truncation, no error).
    wide = b"x" * (512 * 1024)
    src = b"CREATE SCHEMA s;\n" + wide + b"\n"
    out = _rewrite_create_schema(src)
    assert out == b"CREATE SCHEMA IF NOT EXISTS s;\n" + wide + b"\n"
    assert len(out) >= len(wide)


def test_chunk_boundary_carry_preserves_correctness() -> None:
    # Simulate the pump's carry logic: split a dump at an arbitrary byte
    # boundary (mid-line), feed only complete lines per chunk, carry the
    # remainder. The reassembled, rewritten output must equal a single-pass
    # rewrite -- proving the boundary handling is correct.
    dump = b"CREATE SCHEMA a;\n" + b"d" * 100000 + b"\nCREATE SCHEMA b;\nlast line no newline"
    expected = _rewrite_create_schema(dump)

    out = bytearray()
    carry = b""
    for i in range(0, len(dump), 4096):
        data = carry + dump[i : i + 4096]
        nl = data.rfind(b"\n")
        if nl == -1:
            carry = data
            continue
        complete, carry = data[: nl + 1], data[nl + 1 :]
        out += _rewrite_create_schema(complete)
    if carry:
        out += _rewrite_create_schema(carry)

    assert bytes(out) == expected
    assert out.startswith(b"CREATE SCHEMA IF NOT EXISTS a;\n")
    assert b"CREATE SCHEMA IF NOT EXISTS b;\n" in bytes(out)


def test_bounded_carry_flushes_wide_line_without_corruption() -> None:
    # Mirrors the pump's bounded-carry branch: an unterminated line longer
    # than carry_bound is flushed verbatim mid-line, then the rest of the
    # dump continues normally. The reassembled stream must be byte-identical
    # to a single-pass rewrite -- proving the memory bound does not corrupt
    # the CREATE SCHEMA rewrite (a mid-line fragment is never a line-anchored
    # match, so emitting it unmodified is correct).
    chunk = 4096
    carry_bound = 8192
    # A wide single COPY-style data row (no newline for a long stretch),
    # bracketed by real CREATE SCHEMA lines that must still be rewritten.
    dump = b"CREATE SCHEMA a;\n" + (b"v" * 50000) + b"\nCREATE SCHEMA b;\nx" * 2
    expected = _rewrite_create_schema(dump)

    out = bytearray()
    carry = b""
    for i in range(0, len(dump), chunk):
        data = carry + dump[i : i + chunk]
        nl = data.rfind(b"\n")
        if nl == -1:
            if len(data) >= carry_bound:
                out += data  # flush verbatim, no rewrite (mid-line)
                carry = b""
            else:
                carry = data
            continue
        complete, carry = data[: nl + 1], data[nl + 1 :]
        out += _rewrite_create_schema(complete)
    if carry:
        out += _rewrite_create_schema(carry)

    assert bytes(out) == expected
    assert out.startswith(b"CREATE SCHEMA IF NOT EXISTS a;\n")
    assert bytes(out).count(b"CREATE SCHEMA IF NOT EXISTS b;\n") == 2
    assert b"v" * 50000 in bytes(out)
