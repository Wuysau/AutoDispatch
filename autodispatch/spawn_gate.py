"""Windows child waits for containment before starting any requested executable."""
import json
import os
import subprocess
import sys

if __name__ == "__main__":
    # os.read does not prefetch Agent prompt bytes that the child must consume.
    if os.read(sys.stdin.fileno(), 1) != b"G":
        raise SystemExit(125)
    if os.name == "nt":
        import ctypes
        # A detached service has no console; pwsh otherwise falls back to the
        # system OEM code page. The runner provides a hidden private console.
        if not ctypes.windll.kernel32.SetConsoleOutputCP(65001) or not ctypes.windll.kernel32.SetConsoleCP(65001):
            raise SystemExit("Cannot establish UTF-8 subprocess console")
    command = json.loads(sys.argv[1])
    child = subprocess.Popen(command, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr, shell=False)
    raise SystemExit(child.wait())
