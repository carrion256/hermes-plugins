import json

def parse_records(text):
    rows = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            raise ValueError(f"line {number}: expected JSON object")
        if not isinstance(obj, dict):
            raise ValueError(f"line {number}: expected JSON object")
        rows.append(obj)
    return rows
