"""
Guards against module-scope name shadowing in the entrypoint.

`main.py` binds short names at module level — `f` for a file handle in two
places — so any import aliased to a single letter is one `with open(...)` away
from being silently replaced. When that happened, the engine reached its
start-up message, called `f.venue_name(...)` on a TextIOWrapper, and died with
`AttributeError` before the trading loop ever began. Docker reported the
container "healthy" throughout, because the healthcheck only asks the dashboard
whether it is up.

These are static checks, not a run of the bot: importing main.py would open
real connections and start real trading. What is checked is the property that
actually broke — that no name is both imported and reused as a file handle.

Run: python3 -m unittest tests.test_entrypoint_shadowing
"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "main.py"


def _module_level_bindings(tree):
    """Names bound at module scope, i.e. visible to the whole process.

    A binding inside a function or comprehension does not leak, so it cannot
    shadow an import used elsewhere. Only the top level counts.
    """
    bound = {}
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                bound.setdefault(name, []).append(f"import:{name}")
        elif isinstance(node, ast.With):
            for item in node.items:
                if item.optional_vars is not None:
                    for sub in ast.walk(item.optional_vars):
                        if isinstance(sub, ast.Name):
                            bound.setdefault(sub.id, []).append(
                                f"with-open:{sub.id} (line {node.lineno})")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name):
                        bound.setdefault(sub.id, []).append(
                            f"assign:{sub.id} (line {node.lineno})")
    return bound


class EntrypointShadowingTest(unittest.TestCase):
    def setUp(self):
        # encoding is explicit: without it Python uses the locale default, which
        # is cp1252 on Windows and raises UnicodeDecodeError on the em-dashes and
        # box-drawing characters main.py uses in its log messages. The test then
        # fails for a reason that has nothing to do with what it checks.
        self.tree = ast.parse(MAIN.read_text(encoding="utf-8"))
        self.bound = _module_level_bindings(self.tree)

    def test_no_import_alias_is_reused_as_a_file_handle(self):
        # The exact failure: `from alerts import formatting as f` followed by
        # `with open("config/config.yaml") as f` at module level. The second
        # binding wins, and every later `f.something()` is a call on a file.
        offenders = {
            name: kinds for name, kinds in self.bound.items()
            if any(k.startswith("import:") for k in kinds)
            and any(k.startswith("with-open:") for k in kinds)
        }
        self.assertEqual(offenders, {},
                         f"import alias reused as a file handle at module scope: {offenders}")

    def test_formatting_is_imported_under_a_multi_letter_name(self):
        # A single-letter alias is legal but fragile here; the file handle `f`
        # is load-bearing in two places, so the formatting module is pinned to
        # a name that cannot collide with it.
        aliases = [
            alias.asname for node in self.tree.body
            if isinstance(node, ast.ImportFrom) and node.module == "alerts"
            and (alias := node.names[0])
        ]
        self.assertIn("fm", aliases,
                      "alerts.formatting must be imported as `fm`, not `f`")

    def test_every_venue_name_call_resolves_to_the_formatting_module(self):
        # A bare `f.venue_name` would pass every syntax check and still be a
        # file handle at runtime, so the call is matched by its attribute name
        # rather than trusted.
        calls = {
            node.func.value.id
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "venue_name"
            and isinstance(node.func.value, ast.Name)
        }
        self.assertEqual(calls, {"fm"},
                         f"venue_name called on {calls}, expected only the formatting module")
