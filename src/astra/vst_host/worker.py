"""One plugin scanner per process. Invoked only by the VST catalog."""
import json
import sys
from pathlib import Path
from .native import NativeHost

def main():
    if len(sys.argv) not in (4, 5) or sys.argv[1] != "scan":
        raise SystemExit("usage: python -m astra.vst_host.worker scan HOST_DLL PLUGIN [RESULT_JSON]")
    try:
        result = NativeHost(sys.argv[2]).scan(sys.argv[3])
        if len(sys.argv) == 5:
            with Path(sys.argv[4]).open("x", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False)
        else:
            print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
