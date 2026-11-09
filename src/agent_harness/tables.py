"""Bounded CSV operations with decimal arithmetic and physical source lines."""

import csv
from decimal import Decimal, InvalidOperation, localcontext
import io

from .contracts import RunFailure
from .storage import Record


def _column_numbers(rows: list[list[str]], index: int) -> list[Decimal]:
    if not rows:
        raise ValueError
    numbers = []
    non_numeric = 0
    for row in rows:
        raw = row[index].strip()
        if len(raw) > 64:
            raise ValueError
        try:
            number = Decimal(raw)
        except InvalidOperation:
            non_numeric += 1
            continue
        exponent = number.as_tuple().exponent
        if not number.is_finite() or number.copy_abs() > Decimal("1e30") or not isinstance(exponent, int) or exponent < -12:
            raise ValueError
        numbers.append(number)
    if non_numeric:
        if not numbers:
            raise RunFailure("invalid_table", "The selected column contains no decimal values. Inspect the headers and use a numeric column.", correctable_fields=("column",))
        raise ValueError
    return numbers


def _aggregatable(rows: list[list[str]], index: int) -> bool:
    try:
        _column_numbers(rows, index)
    except (ValueError, RunFailure):
        return False
    return True


def table_result(source: Record, column: str | None = None, operation: str = "sum") -> Record:
    try:
        reader = csv.reader(io.StringIO(source["text"], newline=""), strict=True)
        columns = next(reader)
        columns[0] = columns[0].removeprefix("\ufeff")
        if not columns or len(columns) > 64 or any(not name or len(name) > 100 for name in columns) or len(set(columns)) != len(columns):
            raise ValueError
        start = reader.line_num + 1
        rows: list[list[str]] = []
        for row in reader:
            if len(rows) >= 5000 or len(row) != len(columns):
                raise ValueError
            rows.append(row)
        result: Record = {"path": source["path"], "sha256": source["sha256"], "columns": columns,
                          "rows": len(rows), "line_start": start, "line_end": reader.line_num}
        if column is None:
            result["sample"] = rows[:3]
            result["aggregatable_columns"] = [name for index, name in enumerate(columns) if _aggregatable(rows, index)]
            return result
        if column not in columns:
            raise RunFailure("invalid_table", "The requested column is not present. Inspect the headers and use an existing column.", correctable_fields=("column",))
        numbers = _column_numbers(rows, columns.index(column))
        with localcontext() as context:
            context.prec = 64
            value = sum(numbers, Decimal(0))
            if operation == "mean":
                value /= len(numbers)
            result.update({"column": column, "operation": operation, "value": format(value, "f")})
        return result
    except (csv.Error, StopIteration, ValueError, IndexError, InvalidOperation) as error:
        raise RunFailure("invalid_table", "Use a nonempty rectangular CSV with distinct headers and finite decimal values; limits: 5000 rows, 64 columns, 30 integer and 12 fractional digits.") from error


def ratio_percent(numerator: str, denominator: str) -> str:
    try:
        with localcontext() as context:
            context.prec = 64
            bottom = Decimal(denominator)
            if not bottom:
                raise InvalidOperation
            value = Decimal(numerator) / bottom * 100
            return format(value.quantize(Decimal("0.000001")), "f")
    except (InvalidOperation, ArithmeticError) as error:
        raise RunFailure("invalid_calculation", "The percentage requires a nonzero denominator and a bounded decimal result.") from error
