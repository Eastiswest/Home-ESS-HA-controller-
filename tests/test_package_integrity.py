"""Static integrity checks over the integration package.

Home Assistant cannot be installed on every Python version, so the HA-facing
modules (coordinator, config flow, platforms) are not importable in this test
environment. These checks therefore verify statically what an import would
otherwise catch:

* every module parses;
* every relative import inside the package resolves to a name the target module
  actually defines -- the failure mode that would break setup at runtime;
* the manifest, strings and services files are valid and consistent with the
  code.

This is deliberately not a substitute for running the integration in Home
Assistant, but it catches the class of mistake that is easy to make while
refactoring and expensive to discover on a live battery.
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "custom_components" / "ess_controller"

PYTHON_FILES = sorted(PACKAGE.rglob("*.py"))


def module_name_for(path: pathlib.Path) -> str:
    """Dotted module name relative to the package root."""
    relative = path.relative_to(PACKAGE)
    parts = list(relative.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][: -len(".py")]
    return ".".join(parts)


def path_for_module(dotted: str) -> pathlib.Path | None:
    """Resolve a dotted module name back to a file, package or module."""
    if not dotted:
        return PACKAGE / "__init__.py"
    base = PACKAGE.joinpath(*dotted.split("."))
    if (base / "__init__.py").exists():
        return base / "__init__.py"
    candidate = base.with_suffix(".py")
    return candidate if candidate.exists() else None


def top_level_names(tree: ast.Module) -> set[str]:
    """Names a module makes importable: defs, classes, assignments, re-exports."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


TREES: dict[str, ast.Module] = {}
#: Dotted names that are packages (came from an ``__init__.py``) rather than
#: plain modules. The distinction matters for resolving relative imports: a
#: level-1 import inside a package means the package itself, but inside a plain
#: module it means the package *containing* that module.
PACKAGES: set[str] = set()

for _path in PYTHON_FILES:
    _name = module_name_for(_path)
    TREES[_name] = ast.parse(_path.read_text(encoding="utf-8"), filename=str(_path))
    if _path.name == "__init__.py":
        PACKAGES.add(_name)


def resolve_relative(module: str, node: ast.ImportFrom) -> str:
    """Resolve a relative import to a dotted module name within the package."""
    parts = module.split(".") if module else []
    if module not in PACKAGES:
        # A plain module's level-1 anchor is the package that contains it.
        parts = parts[:-1]
    # Each level beyond the first walks one package further up.
    upward = node.level - 1
    base = parts[: len(parts) - upward] if upward else parts
    if node.module:
        base = [*base, *node.module.split(".")]
    return ".".join(base)


class TestParsing:
    @pytest.mark.parametrize("path", PYTHON_FILES, ids=lambda p: p.name)
    def test_module_parses(self, path: pathlib.Path):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_expected_modules_present(self):
        names = set(TREES)
        for required in (
            "",  # the package __init__
            "const",
            "models",
            "coordinator",
            "config_flow",
            "settings",
            "runtime",
            "sampling",
            "entity",
            "diagnostics",
            "sensor",
            "binary_sensor",
            "switch",
            "number",
            "select",
            "button",
            "adjustments",
            "outage",
            "shifting",
            "recommend",
            "optimiser.dp",
            "learning.model",
            "learning.store",
            "forecast.solar",
            "forecast.load",
            "forecast.weather",
            "forecast.energy",
            "tariff.base",
            "tariff.octopus",
            "tariff.entity",
            "tariff.fixed",
            "tariff.factory",
            "inverter.base",
            "inverter.solax",
            "inverter.generic",
            "inverter.battery",
            "inverter.roles",
        ):
            assert required in names, f"missing module {required!r}"


class TestRelativeImports:
    """Every ``from .x import y`` must name something ``x`` really defines."""

    def _collect(self) -> list[tuple[str, str, str, int]]:
        found: list[tuple[str, str, str, int]] = []
        for module, tree in TREES.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level:
                    target = resolve_relative(module, node)
                    for alias in node.names:
                        if alias.name == "*":
                            continue
                        found.append((module, target, alias.name, node.lineno))
        return found

    def test_targets_exist(self):
        missing = [
            (module, target, name, line)
            for module, target, name, line in self._collect()
            if path_for_module(target) is None
        ]
        assert not missing, f"imports from non-existent modules: {missing}"

    def test_names_exist(self):
        problems: list[str] = []
        for module, target, name, line in self._collect():
            target_path = path_for_module(target)
            if target_path is None:
                continue
            target_module = module_name_for(target_path)
            tree = TREES.get(target_module)
            if tree is None:
                continue
            available = top_level_names(tree)
            # A submodule of a package is a legitimate import target too.
            if name in available or path_for_module(f"{target}.{name}") is not None:
                continue
            problems.append(f"{module}:{line} imports {name!r} from {target!r}")
        assert not problems, "unresolved imports:\n" + "\n".join(problems)

    def test_no_circular_import_between_settings_and_runtime(self):
        """Settings must stay free of the persistence layer, or the HA-free
        guarantee that makes it testable is lost."""
        settings_tree = TREES["settings"]
        for node in ast.walk(settings_tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "runtime"


class TestHomeAssistantFreeModules:
    """The planning logic must stay importable without Home Assistant.

    This is what allows the optimiser, learning and tariff parsing to be tested
    on any Python. If an HA import creeps into one of these, the test suite
    silently loses its coverage of the decision-making code.
    """

    HA_FREE = (
        "models",
        "settings",
        "sampling",
        "adjustments",
        "outage",
        "shifting",
        "recommend",
        "performance",
        "optimiser.dp",
        "optimiser",
        "learning.model",
        "learning",
        "forecast.solar",
        "forecast.load",
        "forecast.weather",
        "forecast.energy",
        "forecast",
        "tariff.base",
        "tariff.octopus",
        "tariff.entity",
        "tariff.fixed",
        "tariff",
        "inverter.base",
        "inverter.solax",
        "inverter.generic",
        "inverter.battery",
        "inverter.roles",
        "inverter",
        "const",
    )

    @pytest.mark.parametrize("module", HA_FREE)
    def test_no_homeassistant_import(self, module: str):
        tree = TREES[module]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("homeassistant"), (
                        f"{module} imports {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("homeassistant"), (
                    f"{module} imports from {node.module}"
                )


class TestManifest:
    def test_manifest_is_valid(self):
        manifest = json.loads((PACKAGE / "manifest.json").read_text())
        for key in ("domain", "name", "version", "documentation", "codeowners"):
            assert manifest.get(key), f"manifest missing {key}"
        assert manifest["domain"] == "ess_controller"

    def test_no_external_requirements(self):
        """The optimiser is deliberately dependency-free: nothing to install,
        nothing to break on a Raspberry Pi."""
        manifest = json.loads((PACKAGE / "manifest.json").read_text())
        assert manifest["requirements"] == []

    def test_domain_matches_const(self):
        manifest = json.loads((PACKAGE / "manifest.json").read_text())
        const = TREES["const"]
        for node in const.body:
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == (
                "DOMAIN"
            ):
                assert node.value.value == manifest["domain"]
                return
        pytest.fail("DOMAIN not found in const.py")

    def test_hacs_metadata_present(self):
        hacs = json.loads((ROOT / "hacs.json").read_text())
        assert hacs["name"]


class TestTranslations:
    def test_strings_and_en_match(self):
        strings = json.loads((PACKAGE / "strings.json").read_text())
        english = json.loads((PACKAGE / "translations" / "en.json").read_text())
        assert strings == english, "translations/en.json is out of sync with strings.json"

    def test_every_entity_has_a_translation(self):
        """A missing translation_key shows up as a raw slug in the UI."""
        strings = json.loads((PACKAGE / "strings.json").read_text())
        entity_strings = strings["entity"]

        for platform, module in (
            ("sensor", "sensor"),
            ("binary_sensor", "binary_sensor"),
            ("switch", "switch"),
            ("number", "number"),
            ("button", "button"),
        ):
            keys = _translation_keys(TREES[module])
            assert keys, f"no translation keys found in {module}.py"
            declared = set(entity_strings.get(platform, {}))
            missing = keys - declared
            assert not missing, f"{platform} missing translations for {sorted(missing)}"

    def test_services_documented(self):
        strings = json.loads((PACKAGE / "strings.json").read_text())
        services_yaml = (PACKAGE / "services.yaml").read_text()
        for service in ("replan", "set_override", "clear_override", "reset_learning"):
            assert f"{service}:" in services_yaml, f"{service} missing from services.yaml"
            assert service in strings["services"], f"{service} missing from strings.json"

    def test_selector_options_cover_constants(self):
        """Every provider, adapter and strategy offered must have a label."""
        strings = json.loads((PACKAGE / "strings.json").read_text())
        selectors = strings["selector"]
        const_values = _const_lists(TREES["const"])

        for name, selector_key in (
            ("IMPORT_PROVIDERS", "provider"),
            ("EXPORT_PROVIDERS", "provider"),
            ("ADAPTERS", "adapter"),
            ("PRICE_SCALES", "price_scale"),
        ):
            options = set(selectors[selector_key]["options"])
            missing = set(const_values.get(name, [])) - options
            assert not missing, f"{selector_key} selector missing {sorted(missing)}"

        strategies = set(const_values.get("STRATEGIES", []))
        declared = set(strings["entity"]["select"]["strategy"]["state"])
        assert not strategies - declared


def _translation_keys(tree: ast.Module) -> set[str]:
    """Collect ``translation_key="..."`` values from entity descriptions."""
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "translation_key" and isinstance(
                    keyword.value, ast.Constant
                ):
                    keys.add(keyword.value.value)
    return keys


def _const_lists(tree: ast.Module) -> dict[str, list[str]]:
    """Read the string-list constants out of const.py."""
    found: dict[str, list[str]] = {}
    constants: dict[str, str] = {}

    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            name, value = target.id, node.value
        else:
            continue

        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            constants[name] = value.value
        elif isinstance(value, ast.List):
            items: list[str] = []
            for element in value.elts:
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    items.append(element.value)
                elif isinstance(element, ast.Name) and element.id in constants:
                    items.append(constants[element.id])
            if items:
                found[name] = items
    return found
