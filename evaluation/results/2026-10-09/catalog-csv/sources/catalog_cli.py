import json
import sys

from catalog import read_catalog


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        sys.stderr.write('usage: catalog_cli.py PATH\n')
        return 2
    try:
        with open(argv[0], encoding='utf-8', newline='') as stream:
            data = read_catalog(stream)
    except ValueError:
        sys.stderr.write('invalid catalog\n')
        return 2
    print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
