import csv

_INVALID = object()


def _invalid():
    return ValueError('invalid catalog')


def read_catalog(stream):
    try:
        reader = csv.reader(stream)
        header = next(reader, _INVALID)
    except csv.Error:
        raise _invalid()
    if header is _INVALID or header != ['name', 'quantity']:
        raise _invalid()
    result = {}
    try:
        for row in reader:
            if row == []:
                continue
            if len(row) != 2:
                raise _invalid()
            name = row[0].strip()
            quantity = row[1].strip()
            if not name:
                raise _invalid()
            if not quantity or any(c not in '0123456789' for c in quantity):
                raise _invalid()
            result[name] = result.get(name, 0) + int(quantity)
    except csv.Error:
        raise _invalid()
    return result
