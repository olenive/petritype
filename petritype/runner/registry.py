"""The nets a runner carries: entries built from ``@petri_net`` factories.

A ``NetEntry`` is what ``describe`` lists and ``load`` builds: the decorator's
name, description, ``mode`` and ``schedule``, and the factory's parameters read
off its signature. ``Registry.from_modules`` collects the decorated factories of
modules that are already imported (importing by ``--module`` belongs to the
process layer); ``Registry.resolve_v0`` serves the version 0 ``load {module,
function, factory_params}`` form by importing ``module`` and looking up
``function``.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import inspect
import json
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Callable, Iterable, Optional

from pydantic import TypeAdapter, ValidationError

from petritype.runner import protocol as wire
from petritype.runner.protocol import ErrorCode, ProtocolError
from petritype.runner.translate import TypeNames


@dataclass(frozen=True)
class ParamSpec:
    """One factory parameter. ``default`` is meaningful only when ``has_default``;
    ``json_default`` says whether that default has a JSON form for ``describe``."""

    name: str
    type_name: str
    required: bool
    annotation: Any = inspect.Parameter.empty
    has_default: bool = False
    default: Any = None
    json_default: bool = False


@dataclass(frozen=True)
class NetEntry:
    """A net this runner can build: ``factory(**params)`` returns an ``ExecutableGraph``."""

    name: str
    factory: Callable
    description: Optional[str] = None
    mode: Optional[str] = None
    schedule: Optional[str] = None
    params: tuple[ParamSpec, ...] = ()
    # The factory takes ``**kwargs``, so names it does not declare are passed on.
    accepts_any: bool = False


class NetEntries:
    """Functions that build and use ``NetEntry`` records."""

    def from_factory(factory: Callable, name: Optional[str] = None) -> NetEntry:
        """An entry for ``factory``, decorated with ``@petri_net`` or not. The name
        is ``name``, else the decorator's, else the function's."""
        config = getattr(factory, "_petri_net_config", None) or {}
        description = config.get("description") or NetEntries.first_doc_line(factory)
        params, accepts_any = NetEntries.params_of(factory)
        return NetEntry(
            name=name or config.get("name") or factory.__name__,
            factory=factory,
            description=description or None,
            mode=config.get("mode"),
            schedule=config.get("schedule"),
            params=params,
            accepts_any=accepts_any,
        )

    def first_doc_line(factory: Callable) -> Optional[str]:
        doc = inspect.getdoc(factory)
        if not doc:
            return None
        return doc.strip().splitlines()[0].strip() or None

    def params_of(factory: Callable) -> tuple[tuple[ParamSpec, ...], bool]:
        """The parameters of ``factory``'s signature (``@wraps`` is followed), and
        whether it takes ``**kwargs``. ``*args`` cannot be named, so it is skipped."""
        try:
            signature = inspect.signature(factory)
        except (TypeError, ValueError):
            return (), True
        specs = []
        accepts_any = False
        for parameter in signature.parameters.values():
            if parameter.kind is inspect.Parameter.VAR_KEYWORD:
                accepts_any = True
                continue
            if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
                continue
            has_default = parameter.default is not inspect.Parameter.empty
            json_default = False
            if has_default:
                try:
                    json.dumps(parameter.default, allow_nan=False)
                    json_default = True
                except (TypeError, ValueError):
                    json_default = False
            specs.append(ParamSpec(
                name=parameter.name,
                type_name=TypeNames.of(parameter.annotation),
                required=not has_default,
                annotation=parameter.annotation,
                has_default=has_default,
                default=parameter.default if has_default else None,
                json_default=json_default,
            ))
        return tuple(specs), accepts_any

    def describe(entry: NetEntry) -> wire.NetDescription:
        params = []
        for spec in entry.params:
            fields: dict = {"name": spec.name, "type_name": spec.type_name, "required": spec.required}
            if spec.has_default and spec.json_default:
                fields["default"] = spec.default
            params.append(wire.ParamDescription(**fields))
        return wire.NetDescription(
            name=entry.name,
            description=entry.description,
            mode=entry.mode,
            schedule=entry.schedule,
            params=params,
        )

    def bind(entry: NetEntry, params: dict) -> dict:
        """The keyword arguments for ``entry.factory`` from JSON ``params``, or
        ``ProtocolError(invalid_params)``: an unknown name (unless the factory
        takes ``**kwargs``), a missing required one, or a value that does not
        validate against its annotation (with pydantic, so a model parameter
        decodes from a JSON object). An annotation pydantic cannot handle, or a
        string one, passes the value through unchanged."""
        known = {spec.name: spec for spec in entry.params}
        unknown = sorted(set(params) - set(known))
        if unknown and not entry.accepts_any:
            raise ProtocolError(
                ErrorCode.INVALID_PARAMS,
                f"net {entry.name!r} has no parameter {', '.join(map(repr, unknown))}",
            )
        missing = [spec.name for spec in entry.params if spec.required and spec.name not in params]
        if missing:
            raise ProtocolError(
                ErrorCode.INVALID_PARAMS,
                f"net {entry.name!r} needs parameter {', '.join(map(repr, missing))}",
            )
        bound = {}
        for name, value in params.items():
            spec = known.get(name)
            bound[name] = value if spec is None else NetEntries.decode(spec, value)
        return bound

    def decode(spec: ParamSpec, value: Any) -> Any:
        annotation = spec.annotation
        if annotation is inspect.Parameter.empty or isinstance(annotation, str):
            return value
        try:
            adapter = TypeAdapter(annotation)
        except Exception:
            return value
        try:
            return adapter.validate_python(value)
        except ValidationError as error:
            raise ProtocolError(
                ErrorCode.INVALID_PARAMS,
                f"parameter {spec.name!r} ({spec.type_name}): {error.errors()[0].get('msg', error)}",
            ) from error


@dataclass
class Registry:
    """The nets a runner serves, by name, in the order they were collected."""

    entries: dict[str, NetEntry] = field(default_factory=dict)

    @classmethod
    def from_factories(cls, factories: Iterable[Callable]) -> "Registry":
        registry = cls()
        for factory in factories:
            registry.add(NetEntries.from_factory(factory))
        return registry

    @classmethod
    def from_modules(cls, modules: Iterable[ModuleType]) -> "Registry":
        """Every ``@petri_net`` factory at the top level of ``modules`` (already
        imported), in module order then definition order. The same factory reached
        through two modules counts once; two factories with one name are an error,
        since ``load`` could not tell them apart."""
        registry = cls()
        seen: set[int] = set()  # a factory one module imports from another counts once
        for module in modules:
            for value in vars(module).values():
                if callable(value) and getattr(value, "_petri_net_config", None) and id(value) not in seen:
                    seen.add(id(value))
                    registry.add(NetEntries.from_factory(value))
        return registry

    def add(self, entry: NetEntry) -> None:
        if entry.name in self.entries:
            raise ValueError(f"two nets are named {entry.name!r}")
        self.entries[entry.name] = entry

    def get(self, name: str) -> Optional[NetEntry]:
        return self.entries.get(name)

    def resolve_v0(self, module: str, function: str) -> NetEntry:
        """The version 0 ``load`` form: import ``module`` and take ``function``
        from it. ``unknown_net`` when either does not exist; ``load_failed`` when
        the module raises on import."""
        try:
            imported = importlib.import_module(module)
        except ModuleNotFoundError as error:
            if error.name and (module == error.name or module.startswith(error.name + ".")):
                raise ProtocolError(ErrorCode.UNKNOWN_NET, f"no module named {module!r}") from error
            raise ProtocolError(ErrorCode.LOAD_FAILED, f"importing {module}: {error}") from error
        except Exception as error:
            raise ProtocolError(
                ErrorCode.LOAD_FAILED, f"importing {module}: {type(error).__name__}: {error}"
            ) from error
        factory = getattr(imported, function, None)
        if not callable(factory):
            raise ProtocolError(ErrorCode.UNKNOWN_NET, f"module {module!r} has no function {function!r}")
        return NetEntries.from_factory(factory)

    def describe(self) -> wire.Describe:
        """The ``describe`` answer (PROTOCOL.md §6.1)."""
        return wire.Describe(
            runtime_version=runtime_version(),
            nets=[NetEntries.describe(entry) for entry in self.entries.values()],
        )


def runtime_version() -> str:
    """The installed petritype version, or ``"unknown"`` from a bare checkout."""
    try:
        return importlib.metadata.version("petritype")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"
