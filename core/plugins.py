"""Hot-swap plugin registry for the NIOS 4-ring runtime (Step 8).

The registry lets an operator register a new **factor pack**, **data loader**, or
**broker silo adapter** without restarting a worker.  It does not, however,
grant live capital: every strategy/factor plugin is born inside the
``ShadowExecutionTwin`` at ``0/30`` shadow fills, is enqueued into
``NocturnalAlphaForge`` as a ``CHALLENGER``, and cannot graduate until it owns
30 winning live L2 shadow fills (``sec_018`` Law 4 / Law 5).

The module is part of ``core`` and therefore imports neither Ring 0 signing code
nor Ring 2 implementation classes.  The Forge and the shadow twin are duck-typed
collaborators injected by the composition root.
"""

from __future__ import annotations

import threading
import time
import zlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Mapping

__all__ = [
    "PluginKind",
    "PluginManifest",
    "PluginRecord",
    "PluginRegistry",
    "PluginStatus",
    "REQUIRED_SHADOW_FILLS",
]

#: ``sec_018`` Law 4: 30 live L2 shadow fills before live graduation.
REQUIRED_SHADOW_FILLS: int = 30


class PluginKind(str, Enum):
    """The three hot-swappable plugin classes."""

    FACTOR_PACK = "factor_pack"
    DATA_LOADER = "data_loader"
    BROKER_SILO = "broker_silo"

    @classmethod
    def parse(cls, value: "PluginKind | str") -> "PluginKind":
        if isinstance(value, cls):
            return value
        token = str(value).strip().lower().replace("-", "_")
        for member in cls:
            if token in (member.value, member.name.lower()):
                return member
        raise ValueError(f"unknown plugin kind {value!r}")


class PluginStatus(str, Enum):
    """Plugin lifecycle.  ``SHADOW`` is the mandatory nursery."""

    SHADOW = "SHADOW"
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


@dataclass(frozen=True)
class PluginManifest:
    """Declarative plugin identity and air-gap ownership."""

    name: str
    kind: PluginKind | str
    version: str = "1.0.0"
    description: str = ""
    author: str = "ring3.operator"
    ring: str = "ring2"
    entrypoint: str = ""
    factor_weights: Mapping[str, float] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        token = str(self.name).strip()
        if not token:
            raise ValueError("plugin name must be non-empty")
        object.__setattr__(self, "name", token)
        kind = PluginKind.parse(self.kind)
        object.__setattr__(self, "kind", kind)
        ring = str(self.ring).strip().lower()
        if kind is PluginKind.BROKER_SILO:
            if ring != "ring0":
                raise ValueError(
                    "broker silo plugins must declare ring='ring0'; Ring 3 and "
                    "Ring 2 may never load a credentialed adapter (sec_004)"
                )
        elif ring not in ("ring2", "ring1"):
            raise ValueError(
                f"{kind.value} plugins must declare ring='ring2' (or ring1 for "
                f"feed loaders), got {ring!r}"
            )
        object.__setattr__(self, "ring", ring)
        object.__setattr__(
            self,
            "factor_weights",
            {str(key): float(value) for key, value in dict(self.factor_weights).items()},
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "ring": self.ring,
            "entrypoint": self.entrypoint,
            "factor_count": len(self.factor_weights),
            "metadata": dict(self.metadata),
        }


@dataclass
class PluginRecord:
    """Runtime state of one registered plugin."""

    manifest: PluginManifest
    status: PluginStatus = PluginStatus.SHADOW
    shadow_fills: int = 0
    winning_shadow_fills: int = 0
    graduated: bool = False
    registered_at_ns: int = 0
    activated_at_ns: int = 0
    last_error: str = ""
    factory: Callable[..., Any] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.registered_at_ns = int(self.registered_at_ns or time.time_ns())

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def enabled(self) -> bool:
        return self.status is not PluginStatus.DISABLED

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.manifest.to_dict(),
            "status": self.status.value,
            "enabled": self.enabled,
            "shadow_fills": self.shadow_fills,
            "winning_shadow_fills": self.winning_shadow_fills,
            "required_shadow_fills": REQUIRED_SHADOW_FILLS,
            "graduated": bool(self.graduated),
            "registered_at_ns": self.registered_at_ns,
            "activated_at_ns": self.activated_at_ns,
            "last_error": self.last_error,
        }


def _vector_for_weights(weights: Mapping[str, float], width: int) -> list[float]:
    """Project named factor weights onto the Forge panel width."""
    vector = [0.0] * max(1, int(width))
    for key, value in sorted(weights.items()):
        index = int(zlib.crc32(str(key).encode("utf-8")) % len(vector))
        vector[index] += float(value)
    return vector


class PluginRegistry:
    """Thread-safe hot-swap registry with a mandatory shadow nursery."""

    def __init__(
        self,
        *,
        forge: Any | None = None,
        shadow_twin: Any | None = None,
        on_register: Callable[[PluginRecord], Any] | None = None,
    ) -> None:
        self.forge = forge
        self.shadow_twin = shadow_twin
        self.on_register = on_register
        self._lock = threading.RLock()
        self._plugins: dict[str, PluginRecord] = {}
        self._history: list[dict[str, Any]] = []

    # -- registration -----------------------------------------------------
    def register(
        self,
        manifest: PluginManifest,
        *,
        factory: Callable[..., Any] | None = None,
        activate: bool = True,
    ) -> PluginRecord:
        if not isinstance(manifest, PluginManifest):
            raise TypeError("register expects a PluginManifest")
        with self._lock:
            if manifest.name in self._plugins:
                raise ValueError(f"plugin {manifest.name!r} is already registered")
            record = PluginRecord(
                manifest=manifest,
                status=PluginStatus.SHADOW,
                factory=factory,
            )
            self._plugins[manifest.name] = record
            self._history.append(
                {
                    "event": "REGISTERED",
                    "name": manifest.name,
                    "kind": manifest.kind.value,
                    "ring": manifest.ring,
                    "timestamp_ns": record.registered_at_ns,
                }
            )
            if activate:
                record.activated_at_ns = time.time_ns()
            self._enqueue_challenger(record)
            if self.on_register is not None:
                try:
                    self.on_register(record)
                except Exception as exc:  # callback failure is recorded, not fatal
                    record.last_error = f"{type(exc).__name__}: {exc}"
            return record

    def _enqueue_challenger(self, record: PluginRecord) -> None:
        if record.manifest.kind is PluginKind.BROKER_SILO:
            # Broker adapters prove venue health in shadow, but never become a
            # factor-weight genome: they are not strategies.
            return
        forge = self.forge
        if forge is None or not hasattr(forge, "enqueue_challenger"):
            return
        dataset = getattr(forge, "dataset", None)
        width = 64
        if dataset is not None and getattr(dataset, "factor_returns", None) is not None:
            width = int(dataset.factor_returns.shape[1])
        weights = record.manifest.factor_weights
        if not weights:
            weights = {f"plugin_{record.name}_bias": 1.0}
        try:
            from ring2_brain.forge.alpha_forge import StrategyGenome

            genome = StrategyGenome(
                genome_id=f"plugin:{record.name}",
                weights=_vector_for_weights(weights, width),
                silo=str(record.manifest.metadata.get("silo", "INDIA_ZERODHA_DHAN")),
                shadow_fills=record.shadow_fills,
                winning_shadow_fills=record.winning_shadow_fills,
            )
            forge.enqueue_challenger(genome)
        except Exception as exc:  # pragma: no cover - malformed forge dependency
            record.last_error = f"{type(exc).__name__}: {exc}"

    def register_plugin(
        self,
        manifest: PluginManifest,
        *,
        factory: Callable[..., Any] | None = None,
        activate: bool = True,
    ) -> PluginRecord:
        """Alias for :meth:`register` (operator vocabulary)."""
        return self.register(manifest, factory=factory, activate=activate)

    def unregister(self, name: str) -> bool:
        with self._lock:
            token = str(name)
            record = self._plugins.pop(token, None)
            if record is None:
                return False
            record.status = PluginStatus.DISABLED
            self._history.append(
                {
                    "event": "UNREGISTERED",
                    "name": token,
                    "timestamp_ns": time.time_ns(),
                }
            )
            return True

    # -- lifecycle --------------------------------------------------------
    def get(self, name: str) -> PluginRecord | None:
        with self._lock:
            return self._plugins.get(str(name))

    def list_plugins(self) -> list[dict[str, Any]]:
        with self._lock:
            return [record.to_dict() for record in self._plugins.values()]

    def list(self) -> list[dict[str, Any]]:
        return self.list_plugins()

    def toggle_plugin(self, name: str, enabled: bool) -> dict[str, Any]:
        return self.toggle(name, enabled)

    def record_fill(self, name: str, net_bps: float = 0.0) -> dict[str, Any]:
        return self.record_shadow_fill(name, net_bps)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            plugins = [record.to_dict() for record in self._plugins.values()]
            return {
                "count": len(plugins),
                "active_count": sum(1 for item in plugins if item["enabled"]),
                "graduated_count": sum(1 for item in plugins if item["graduated"]),
                "shadow_count": sum(
                    1 for item in plugins if item["status"] == PluginStatus.SHADOW.value
                ),
                "plugins": plugins,
                "required_shadow_fills": REQUIRED_SHADOW_FILLS,
                "history": list(self._history[-50:]),
            }

    def toggle(self, name: str, enabled: bool) -> dict[str, Any]:
        with self._lock:
            record = self._plugins.get(str(name))
            if record is None:
                raise KeyError(f"unknown plugin {name!r}")
            if enabled:
                record.status = (
                    PluginStatus.ACTIVE
                    if record.graduated
                    else PluginStatus.SHADOW
                )
                record.activated_at_ns = time.time_ns()
            else:
                record.status = PluginStatus.DISABLED
            self._history.append(
                {
                    "event": "TOGGLED",
                    "name": record.name,
                    "enabled": bool(enabled),
                    "timestamp_ns": time.time_ns(),
                }
            )
            return record.to_dict()

    def activate(self, name: str) -> dict[str, Any]:
        return self.toggle(name, True)

    def deactivate(self, name: str) -> dict[str, Any]:
        return self.toggle(name, False)

    # -- shadow graduation -------------------------------------------------
    def _sync_forge(self, record: PluginRecord) -> None:
        forge = self.forge
        if forge is None or not hasattr(forge, "get_challenger"):
            return
        challenger = forge.get_challenger(f"plugin:{record.name}")
        if challenger is None:
            return
        challenger.shadow_fills = max(
            int(challenger.shadow_fills), int(record.shadow_fills)
        )
        challenger.winning_shadow_fills = max(
            int(challenger.winning_shadow_fills), int(record.winning_shadow_fills)
        )

    def record_shadow_fill(self, name: str, net_bps: float = 0.0) -> dict[str, Any]:
        """Record one live L2 shadow fill for a plugin.

        A plugin graduates only at ``>= 30`` completed fills **and** ``>= 30``
        winning fills.  Until then ``graduated`` is ``False`` and the plugin
        cannot be promoted by the Forge.
        """
        with self._lock:
            record = self._plugins.get(str(name))
            if record is None:
                raise KeyError(f"unknown plugin {name!r}")
            record.shadow_fills += 1
            if float(net_bps) > 0.0:
                record.winning_shadow_fills += 1
            record.graduated = (
                record.shadow_fills >= REQUIRED_SHADOW_FILLS
                and record.winning_shadow_fills >= REQUIRED_SHADOW_FILLS
            )
            # Shadow graduation is not live promotion: the Forge still has to
            # certify Deflated Sharpe >= 1.50 before capital is armed.
            if record.status is not PluginStatus.DISABLED:
                record.status = PluginStatus.SHADOW
            if record.graduated:
                self._history.append(
                    {
                        "event": "SHADOW_GRADUATED",
                        "name": record.name,
                        "shadow_fills": record.shadow_fills,
                        "winning_shadow_fills": record.winning_shadow_fills,
                        "timestamp_ns": time.time_ns(),
                    }
                )
            self._sync_forge(record)
            return record.to_dict()

    def sync_shadow_twin(self, name: str | None = None) -> int:
        """Pull completed fills from the injected ``ShadowExecutionTwin``."""
        twin = self.shadow_twin
        if twin is None or not hasattr(twin, "fills"):
            return 0
        updated = 0
        with self._lock:
            records: Iterable[PluginRecord]
            if name is None:
                records = tuple(self._plugins.values())
            else:
                record = self._plugins.get(str(name))
                records = (record,) if record is not None else ()
            for record in records:
                token = f"plugin:{record.name}"
                try:
                    fills = tuple(twin.fills(token))
                except Exception:  # pragma: no cover - injected twin failure
                    continue
                if not fills:
                    continue
                wins = sum(
                    1
                    for fill in fills
                    if float(getattr(fill, "realized_net_bps", 0.0)) > 0.0
                )
                if len(fills) == record.shadow_fills and wins == record.winning_shadow_fills:
                    continue
                record.shadow_fills = len(fills)
                record.winning_shadow_fills = wins
                record.graduated = (
                    record.shadow_fills >= REQUIRED_SHADOW_FILLS
                    and record.winning_shadow_fills >= REQUIRED_SHADOW_FILLS
                )
                self._sync_forge(record)
                updated += 1
        return updated

    # -- reads -------------------------------------------------------------
    def active_plugins(self, kind: PluginKind | str | None = None) -> list[dict[str, Any]]:
        target = PluginKind.parse(kind) if kind is not None else None
        with self._lock:
            return [
                record.to_dict()
                for record in self._plugins.values()
                if record.enabled
                and (target is None or record.manifest.kind is target)
            ]

    def is_graduated(self, name: str) -> bool:
        record = self.get(name)
        return bool(record is not None and record.graduated)

    def shadow_report(self, name: str) -> dict[str, Any]:
        record = self.get(name)
        if record is None:
            raise KeyError(f"unknown plugin {name!r}")
        return {
            "name": record.name,
            "shadow_fills": record.shadow_fills,
            "winning_shadow_fills": record.winning_shadow_fills,
            "required_shadow_fills": REQUIRED_SHADOW_FILLS,
            "remaining_fills": max(
                0, REQUIRED_SHADOW_FILLS - record.shadow_fills
            ),
            "graduated": record.graduated,
            "forge_challenger": f"plugin:{record.name}",
        }
