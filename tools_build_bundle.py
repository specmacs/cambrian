"""Bundle the package into ONE runnable file.

Built because the owner's machine has Python but no git, and a repo checkout was
the only way to run `python -m cambrian`. A single file removes the whole class
of setup problem: no clone, no branch, no path, no working directory.

Sources are embedded and served through a meta-path finder, so relative imports
inside the package keep working exactly as they do from a checkout.
"""
import base64
import datetime
import json
import pathlib
import subprocess
import zlib

root = pathlib.Path(__file__).parent
mods = {}
for f in sorted(root.glob("cambrian/**/*.py")):
    if "__pycache__" in str(f):
        continue
    name = str(f.relative_to(root)).replace("\\", "/")[:-3].replace("/", ".")
    if name.endswith(".__init__"):
        name = name[:-9]
    mods[name] = f.read_text(encoding="utf8")

# Stamp the build with the commit it came from. A bundle is copied by hand, so a
# stale copy runs silently and looks like a bug in the new code — the stamp makes
# "is this the new file?" answerable from the output itself.
_sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root,
                      capture_output=True, text=True).stdout.strip() or "unknown"
_when = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
mods["cambrian.build"] = 'BUILD = "%s built %s"\n' % (_sha, _when)

blob = base64.b64encode(zlib.compress(json.dumps(mods).encode(), 9)).decode()
out = pathlib.Path(root / "dist" / "cambrian_all_in_one.py")
out.parent.mkdir(exist_ok=True)
out.write_text('''#!/usr/bin/env python3
"""CAMBRIAN — the whole desk in one file.

No repo, no git, no install beyond `pip install requests`. Run it from anywhere:

    python cambrian_all_in_one.py --help
    python cambrian_all_in_one.py vault
    python cambrian_all_in_one.py sweep --bankroll 20
    python cambrian_all_in_one.py trade-vault --max-usd 8

The package sources are embedded below and served through a meta-path finder, so
every relative import inside the package resolves the same way it does from a
checkout. This file is GENERATED — edit the repo, not this.
"""
import base64, importlib.abc, importlib.util, json, sys, types, zlib

_SOURCES = json.loads(zlib.decompress(base64.b64decode(_BLOB := """%s""")).decode())


class _Loader(importlib.abc.Loader):
    def __init__(self, name): self.name = name
    def create_module(self, spec): return None
    def exec_module(self, module):
        exec(compile(_SOURCES[self.name], "<%%s>" %% self.name, "exec"), module.__dict__)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname not in _SOURCES:
            return None
        is_pkg = any(k.startswith(fullname + ".") for k in _SOURCES)
        spec = importlib.util.spec_from_loader(fullname, _Loader(fullname), is_package=is_pkg)
        if is_pkg:
            spec.submodule_search_locations = []
        return spec


sys.meta_path.insert(0, _Finder())

if __name__ == "__main__":
    try:
        import requests  # noqa: F401
    except ImportError:
        print("This needs one package. Run:  pip install requests")
        raise SystemExit(2)
    from cambrian.cli import main
    raise SystemExit(main())
''' % blob, encoding="utf8")
print("wrote %s  (%.0f KB)" % (out, out.stat().st_size / 1024))
