from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator, TextIO


@dataclass(frozen=True, slots=True)
class Position:
    """1-based source coordinate."""
    line: int
    column: int


class UnclosedBraceSyntaxError(SyntaxError):
    """Raised when an opening brace has no matching closing brace."""

    def __init__(
        self,
        *,
        termination: Position,
        origin: Position,
    ) -> None:
        self.termination = termination
        self.origin = origin

        super().__init__(
            f"unclosed '{{' sequence: "
            f"termination at line {termination.line}, column {termination.column}; "
            f"opening '{{' originated at line {origin.line}, column {origin.column}"
        )


class UnexpectedClosingBraceSyntaxError(SyntaxError):
    """Raised when a closing brace has no corresponding opening brace."""

    def __init__(self, position: Position) -> None:
        self.position = position
        super().__init__(
            f"unexpected '}}' at line {position.line}, "
            f"column {position.column}"
        )


class BraceValidator:
    """
    Deterministic streaming validator for brace-enclosed sequences.

    Coordinates are 1-based:
      - line 1 is the first input line
      - column 1 is the first character of a line

    The validator does not retain the input buffer. Its auxiliary memory is
    O(maximum brace nesting depth).
    """

    __slots__ = ("_stack", "_line")

    def __init__(self) -> None:
        # Each entry is the Position of its corresponding '{'.
        self._stack: list[Position] = []
        self._line = 0

    def feed_line(self, line: str) -> None:
        """
        Validate one line.

        The caller may supply lines with or without their trailing newline.
        """
        self._line += 1

        # enumerate(..., start=1) avoids maintaining a separate character
        # counter and gives the exact 1-based source column.
        for column, char in enumerate(line, start=1):
            if char == "{":
                self._stack.append(Position(self._line, column))

            elif char == "}":
                if not self._stack:
                    raise UnexpectedClosingBraceSyntaxError(
                        Position(self._line, column)
                    )

                self._stack.pop()

    def finish(self) -> None:
        """
        Validate end-of-input.

        The most recently opened brace is the first one that cannot be
        matched, so its origin is reported directly.
        """
        if self._stack:
            origin = self._stack[-1]

            raise UnclosedBraceSyntaxError(
                termination=Position(self._line, 0),
                origin=origin,
            )

    def validate(self, lines: Iterable[str]) -> None:
        """Validate an arbitrary iterable of text lines."""
        for line in lines:
            self.feed_line(line)

        self.finish()


def validate_stream(stream: TextIO) -> None:
    """
    Validate a potentially large text stream without loading it into memory.
    """
    BraceValidator().validate(stream)


def validate_text(text: str) -> None:
    """Convenience API for an already-materialized string."""
    BraceValidator().validate(text.splitlines(keepends=True))


if __name__ == "__main__":
    import sys

    validate_stream(sys.stdin)
