#!/usr/bin/env python3
"""
Low-overhead diagnostic log tick loop.

Standard library only.

Behavior:
    1. Read a diagnostic text log sequentially.
    2. Identify lines containing configured discrepancy strings.
    3. Parse key=value fields from matching lines.
    4. Store the parsed record in a local dictionary.
    5. Append the record as one JSON object to an output diagnostics file.
    6. Use a lock so multiple threads can safely invoke the writer.

Example matching input:

    2026-09-20T12:20:01Z SystemValidationVariance component=network expected=10 actual=12

Output:

    {"timestamp":"2026-09-20T12:20:01Z","message":"SystemValidationVariance","component":"network","expected":"10","actual":"12"}

The implementation deliberately processes one line at a time.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Dict, Iterable, Optional, TextIO


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_DISCREPANCIES = frozenset(
    (
        "SystemValidationVariance",
    )
)

# One lock per process/output path.  The lock protects writes performed by
# threads using this module.  O_APPEND additionally makes the append intent
# explicit at the operating-system level.
_OUTPUT_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_fields(text: str) -> Dict[str, str]:
    """
    Parse whitespace-separated key=value fields.

    Example:
        "component=network expected=10 actual=12"

    Values may be JSON-style quoted strings:

        message="validation failed" component=network

    Invalid/non key=value tokens are ignored.

    The returned dictionary is intentionally small and short-lived.
    """
    fields: Dict[str, str] = {}
    length = len(text)
    pos = 0

    while pos < length:
        # Skip whitespace.
        while pos < length and text[pos].isspace():
            pos += 1

        if pos >= length:
            break

        # Find '='.
        key_start = pos
        while pos < length and not text[pos].isspace() and text[pos] != "=":
            pos += 1

        if pos >= length or text[pos] != "=":
            # Skip the non-field token.
            while pos < length and not text[pos].isspace():
                pos += 1
            continue

        key = text[key_start:pos]
        pos += 1

        if not key:
            continue

        # Quoted value.
        if pos < length and text[pos] == '"':
            pos += 1
            value_start = pos

            escaped = False
            value_parts = []

            while pos < length:
                char = text[pos]

                if escaped:
                    value_parts.append(char)
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    break
                else:
                    value_parts.append(char)

                pos += 1

            value = "".join(value_parts)

            if pos < length and text[pos] == '"':
                pos += 1
        else:
            value_start = pos

            while pos < length and not text[pos].isspace():
                pos += 1

            value = text[value_start:pos]

        fields[key] = value

    return fields


def _identify_line(
    line: str,
    discrepancy_strings: Iterable[str],
) -> Optional[Dict[str, str]]:
    """
    Return a structured record for a matching line, otherwise None.

    Expected general format:

        <timestamp> <discrepancy> key=value key=value ...

    The parser intentionally does not require a particular timestamp syntax;
    diagnostic formats often differ between systems.
    """
    stripped = line.strip()

    if not stripped:
        return None

    matched = None

    for discrepancy in discrepancy_strings:
        if discrepancy in stripped:
            matched = discrepancy
            break

    if matched is None:
        return None

    record: Dict[str, str] = {
        "message": matched,
    }

    # Preserve a simple leading token as a timestamp when present.
    first_space = stripped.find(" ")

    if first_space > 0:
        first_token = stripped[:first_space]

        # Avoid treating the discrepancy itself as a timestamp.
        if first_token != matched:
            record["timestamp"] = first_token

    # Parse the portion following the discrepancy identifier.
    discrepancy_pos = stripped.find(matched)
    remainder = stripped[discrepancy_pos + len(matched):].strip()

    if remainder:
        record.update(_parse_fields(remainder))

    return record


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _append_json_line(
    output_path: str,
    record: Dict[str, str],
) -> None:
    """
    Append exactly one JSON object to output_path.

    A single process-local lock prevents concurrent threads from interleaving
    writes.  UTF-8 is used explicitly and newline-delimited JSON keeps the
    output stream append-friendly.
    """
    payload = json.dumps(
        record,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    with _OUTPUT_LOCK:
        with open(
            output_path,
            "a",
            encoding="utf-8",
            buffering=1,
        ) as output:
            output.write(payload)
            output.write("\n")


# ---------------------------------------------------------------------------
# Main tick
# ---------------------------------------------------------------------------

def diagnostic_tick(
    input_path: str,
    output_path: str,
    discrepancy_strings: Iterable[str] = DEFAULT_DISCREPANCIES,
    *,
    encoding: str = "utf-8",
) -> int:
    """
    Process one diagnostic log sequentially.

    Returns:
        Number of discrepancy records appended.

    Memory characteristics:
        - The input file is never loaded into memory.
        - Only the current input line and its parsed record are retained.
        - The output is appended incrementally.

    Thread safety:
        - Multiple threads may call diagnostic_tick().
        - Output records are protected by a process-local lock.
    """
    count = 0

    # Materialize the discrepancy set once per tick.  This prevents repeatedly
    # consuming a generator supplied by the caller.
    discrepancies = tuple(discrepancy_strings)

    with open(
        input_path,
        "r",
        encoding=encoding,
        errors="replace",
        buffering=1,
    ) as source:
        for line in source:
            record = _identify_line(line, discrepancies)

            if record is None:
                continue

            _append_json_line(output_path, record)
            count += 1

    return count


# ---------------------------------------------------------------------------
# Optional continuously-running tick loop
# ---------------------------------------------------------------------------

def run_diagnostic_loop(
    input_path: str,
    output_path: str,
    discrepancy_strings: Iterable[str] = DEFAULT_DISCREPANCIES,
    *,
    interval_seconds: float = 1.0,
    encoding: str = "utf-8",
    stop_event: Optional[threading.Event] = None,
) -> None:
    """
    Repeatedly process the diagnostic file.

    This baseline implementation starts each tick from the beginning of the
    file.  For a continuously growing log, use a persistent byte offset if
    duplicate processing must be avoided.

    stop_event:
        Optional threading.Event.  When supplied, setting it terminates the
        loop promptly.
    """
    if interval_seconds < 0:
        raise ValueError("interval_seconds must be >= 0")

    event = stop_event or threading.Event()

    while not event.is_set():
        diagnostic_tick(
            input_path,
            output_path,
            discrepancy_strings,
            encoding=encoding,
        )

        if interval_seconds:
            event.wait(interval_seconds)


# ---------------------------------------------------------------------------
# Incremental growing-file variant
# ---------------------------------------------------------------------------

def follow_diagnostic_log(
    input_path: str,
    output_path: str,
    discrepancy_strings: Iterable[str] = DEFAULT_DISCREPANCIES,
    *,
    interval_seconds: float = 1.0,
    encoding: str = "utf-8",
    stop_event: Optional[threading.Event] = None,
) -> None:
    """
    Follow a growing diagnostic log without repeatedly processing old lines.

    The current byte offset is retained in memory.  If the input file is
    truncated or replaced with a smaller file, the offset is reset.

    This is generally preferable for a logging tick loop because CPU and I/O
    usage remain proportional to newly written data rather than total log size.
    """
    if interval_seconds < 0:
        raise ValueError("interval_seconds must be >= 0")

    event = stop_event or threading.Event()
    discrepancies = tuple(discrepancy_strings)

    offset = 0
    inode = None

    while not event.is_set():
        try:
            stat = os.stat(input_path)
        except OSError:
            event.wait(interval_seconds)
            continue

        # Detect truncation or log rotation/replacement.
        current_inode = getattr(stat, "st_ino", None)

        if (
            inode is not None
            and current_inode != inode
        ):
            offset = 0

        if stat.st_size < offset:
            offset = 0

        inode = current_inode

        try:
            with open(
                input_path,
                "r",
                encoding=encoding,
                errors="replace",
                buffering=1,
            ) as source:
                source.seek(offset)

                while not event.is_set():
                    line = source.readline()

                    if not line:
                        break

                    offset = source.tell()

                    record = _identify_line(
                        line,
                        discrepancies,
                    )

                    if record is not None:
                        _append_json_line(
                            output_path,
                            record,
                        )

        except (OSError, UnicodeError):
            # The file can legitimately disappear or rotate between stat()
            # and open().  Retry on the next tick.
            pass

        event.wait(interval_seconds)


# ---------------------------------------------------------------------------
# Execution handle
# ---------------------------------------------------------------------------

def main() -> int:
    """
    Minimal command-line execution handle.

    Usage:

        python diagnostics.py input.log diagnostics.jsonl

    Optional discrepancy strings may follow the output path:

        python diagnostics.py input.log diagnostics.jsonl \
            SystemValidationVariance DiskValidationVariance
    """
    import sys

    if len(sys.argv) < 3:
        print(
            "usage: diagnostics.py INPUT_LOG OUTPUT_JSONL [DISCREPANCY ...]",
            file=sys.stderr,
        )
        return 2

    input_path = sys.argv[1]
    output_path = sys.argv[2]

    if len(sys.argv) > 3:
        discrepancies = tuple(sys.argv[3:])
    else:
        discrepancies = DEFAULT_DISCREPANCIES

    try:
        count = diagnostic_tick(
            input_path,
            output_path,
            discrepancies,
        )
    except OSError as exc:
        print(
            "diagnostic processing failed: {}".format(exc),
            file=sys.stderr,
        )
        return 1

    print("appended {} diagnostic record(s)".format(count))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
