from dataclasses import dataclass

from sheetsync.codecs import Cell


class SheetsyncError(Exception):
    pass


class SchemaError(SheetsyncError):
    pass


class ApiError(SheetsyncError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


class SourceError(SheetsyncError):
    pass


class LockTimeout(SheetsyncError):
    pass


@dataclass(frozen=True)
class RowError:
    row_number: int
    field: str | None
    raw: Cell
    message: str


@dataclass(frozen=True)
class RowWarning:
    row_number: int
    field: str
    raw: Cell
    matched_format: str


class RowDecodeError(SheetsyncError):
    """Raised by `read(strict=True)` on the first undecodable row."""

    def __init__(self, error: RowError) -> None:
        super().__init__(f"row {error.row_number}: {error.message}")
        self.error = error
