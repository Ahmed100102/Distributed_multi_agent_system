import py_compile
from pathlib import Path

def main():
    errs = 0
    for p in Path('src').rglob('*.py'):
        try:
            py_compile.compile(str(p), doraise=True)
        except Exception as e:
            print('FAILED', p, e)
            errs += 1
    print('Done. syntax errors:', errs)
    return errs

if __name__ == '__main__':
    raise SystemExit(main())
