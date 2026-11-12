import hashlib
import json
import re
from datetime import date, timedelta
from pathlib import Path


class Invalid(ValueError):
    pass


class Conflict(ValueError):
    pass


def text(value, field, maximum=200):
    if not isinstance(value, str):
        raise Invalid(field + ' requires text')
    if not value or len(value) > maximum or value.strip() != value:
        raise Invalid(field + ' invalid length or whitespace')
    if any(ord(char) < 32 for char in value):
        raise Invalid(field + ' contains control characters')
    return value


def identifier(value, field='id'):
    text(value, field, 100)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]*', value):
        raise Invalid(field + ' invalid identifier')
    return value


def integer(value, field, minimum=0, maximum=1000000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise Invalid(field + ' integer out of range')
    return value


def day(value, field='date'):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise Invalid(field + ' requires ISO date')
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise Invalid(field + ' invalid date') from error
    if not 2000 <= parsed.year <= 2100:
        raise Invalid(field + ' unsupported year')
    return parsed


def days(start, end):
    difference = (day(end) - day(start)).days
    if difference < 0:
        raise Invalid('negative interval')
    return difference


def shift(value, count):
    integer(count, 'days', -36500, 36500)
    return (day(value) + timedelta(days=count)).isoformat()


def fields(value, required, optional=()):
    if not isinstance(value, dict):
        raise Invalid('record must be object')
    missing = set(required) - value.keys()
    unknown = value.keys() - set(required) - set(optional)
    if missing or unknown:
        raise Invalid('record fields mismatch: ' + str(sorted(missing | unknown)))
    return value


def records(value, field, maximum=100000):
    if not isinstance(value, list) or len(value) > maximum:
        raise Invalid(field + ' requires bounded array')
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def load(path, maximum=50000000):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise Invalid('bounded regular input file required')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Invalid('duplicate JSON key ' + key)
            result[key] = value
        return result
    def bad_number(value):
        raise Invalid('nonfinite JSON number')
    return json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=unique, parse_constant=bad_number)


def ratio(numerator, denominator):
    from fractions import Fraction
    if denominator == 0:
        return None
    value = Fraction(numerator, denominator)
    return {'numerator': value.numerator, 'denominator': value.denominator}


def unique_rows(rows, field):
    result = {}
    duplicates = 0
    for row in rows:
        key = row['id']
        if key in result:
            if result[key] != row:
                raise Conflict(field + ' conflicting duplicate ' + key)
            duplicates += 1
        else:
            result[key] = row
    return result, duplicates
