from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

from . import config

EFFORTS = ("low", "medium", "high", "xhigh", "max")

MODELS_FILE = Path(__file__).with_name("models.toml")

MODEL_TOKEN = re.compile(r"^@([a-z0-9]+)(\[[^\]]+\])?$")

MODEL_OPTIONS = ("--model", "-m")

EFFORT_OPTIONS = ("--effort",)


class UnknownFamily(ValueError):
    pass


class ModelPinsUnreadable(UnknownFamily):
    pass


def model_pins() -> dict:
    try:
        with open(MODELS_FILE, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise ModelPinsUnreadable(f"{MODELS_FILE}: the model pins cannot be read: {err}") from err


def families() -> dict[str, str]:
    table = model_pins().get("families")
    if not isinstance(table, dict):
        raise ModelPinsUnreadable(f"{MODELS_FILE}: no [families] table")
    for name, concrete in table.items():
        if not isinstance(concrete, str):
            raise ModelPinsUnreadable(f"{MODELS_FILE}: [families] {name} is not a string")
    return dict(table)


def default_model(key: str) -> str:
    table = model_pins().get("defaults", {})
    if not isinstance(table, dict):
        raise ModelPinsUnreadable(f"{MODELS_FILE}: [defaults] is not a table")
    value = table.get(key)
    if not isinstance(value, str):
        raise ModelPinsUnreadable(f"{MODELS_FILE}: [defaults] {key} is missing or not a string")
    return value


def resolve_model(value):
    if not isinstance(value, str):
        return value
    token = value.strip()
    if not token.startswith("@"):
        return value
    known = families()
    match = MODEL_TOKEN.match(token)
    if match is None:
        raise UnknownFamily(
            f"{value!r} is not a model token. The form is @<family> or @<family>[<variant>], "
            f"where <family> is one of {sorted(known)}.")
    family, variant = match.group(1), match.group(2) or ""
    concrete = known.get(family)
    if concrete is None:
        raise UnknownFamily(
            f"{value!r} names model family {family!r}, which is not in the map. "
            f"Known families: {sorted(known)}. The map is [families] in "
            f"{MODELS_FILE}; it has no operator layer by design.")
    return concrete + variant


def map_option_args(args, options, fn) -> list:
    out: list = []
    take_next = False
    for arg in args:
        if take_next:
            out.append(fn(arg))
            take_next = False
            continue
        if arg in options:
            out.append(arg)
            take_next = True
            continue
        option = next((o for o in options if arg.startswith(f"{o}=")), None)
        if option is not None:
            out.append(f"{option}={fn(arg[len(option) + 1:])}")
            continue
        out.append(arg)
    return out


def resolve_model_args(args) -> list:
    return map_option_args(args, MODEL_OPTIONS, resolve_model)


CONCRETE_MODEL = re.compile(r"^claude-([a-z0-9]+)-[a-z0-9.-]+(\[[^\]]+\])?$")


def concrete_family(value: str) -> str | None:
    match = CONCRETE_MODEL.match(value.strip().lower())
    return match.group(1) if match else None


ONE_M_SUFFIX = "[1m]"

DATE_SUFFIX = re.compile(r"-\d{8}$")


def no_native_1m() -> frozenset[str]:
    table = model_pins().get("context", {})
    if not isinstance(table, dict):
        raise ModelPinsUnreadable(f"{MODELS_FILE}: [context] is not a table")
    ids = table.get("no_native_1m", [])
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise ModelPinsUnreadable(f"{MODELS_FILE}: [context] no_native_1m is not a list of model ids")
    return frozenset(i.strip().lower() for i in ids)


def model_key(value: str) -> str:
    key = value.strip().lower()
    if not key.endswith(ONE_M_SUFFIX):
        return key
    bare = key[:-len(ONE_M_SUFFIX)]
    return key if DATE_SUFFIX.sub("", bare) in no_native_1m() else bare


def same_model(running: str, specced: str) -> bool:
    return model_key(running) == model_key(specced)


def model_value_problem(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return "is empty"
    if value.strip().startswith("@"):
        try:
            resolve_model(value)
        except UnknownFamily as err:
            return f"does not resolve: {err}"
        return None
    family = concrete_family(value)
    if family is None:
        return ("is neither a @<family> token nor a concrete claude-<family>-<version> id; a bare alias resolves "
                "to whatever the release calls that tier's latest")
    if family not in families():
        return f"names family {family!r}, which is not in the map; known families: {sorted(families())}"
    return None


MODEL_USAGE = ("usage: dockwright model resolve <@family[variant]|concrete-id>\n"
               "       dockwright model manager [<running model id>]")


def model_main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if args == ["manager"]:
            print(config.manager_model())
            return 0
        if len(args) == 2 and args[0] == "manager" and args[1].strip() and not args[1].lstrip().startswith("-"):
            resolved = config.manager_model()
            print(f"{'same' if same_model(args[1], resolved) else 'differs'} {resolved}")
            return 0
        if len(args) == 2 and args[0] == "resolve":
            print(resolve_model(args[1]))
            return 0
    except UnknownFamily as err:
        print(str(err), file=sys.stderr)
        return 1
    print(MODEL_USAGE, file=sys.stderr)
    return 2
