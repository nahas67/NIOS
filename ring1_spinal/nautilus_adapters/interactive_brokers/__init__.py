"""Interactive Brokers adapter for Ring 1 (NIOS compatibility build).

Harvested from ``nautilus_trader.adapters.interactive_brokers``.  The upstream
package is pure Python and can import on a healthy host, but it still pulls in
``nautilus_trader.model`` which is unavailable when the compiled extensions are
policy-blocked.  The public surface is therefore re-expressed through
:mod:`ring1_spinal.nautilus_adapters._nautilus_compat`.

IBKR carries the US silo alongside Alpaca: T+1 settlement, Reg-T margin guards,
sub-penny maker pegging and SEC EDGAR point-in-time gating.
"""

from __future__ import annotations

from .._nautilus_compat import adapter_exports

__all__ = [
    "ContainerStatus",
    "DockerizedIBGateway",
    "DockerizedIBGatewayConfig",
    "HistoricalInteractiveBrokersClient",
    "IbAction",
    "IbArticleType",
    "IbAuctionStrategy",
    "IbAuctionType",
    "IbBondIdentifierKind",
    "IbBuilderTimeInForce",
    "IbComboLegOpenClose",
    "IbConditionConjunction",
    "IbConditionKind",
    "IbExerciseAction",
    "IbFundAssetType",
    "IbFundDistributionPolicyIndicator",
    "IbHistoricalBarSize",
    "IbHistoricalTickType",
    "IbHistoricalWhatToShow",
    "IbLegAction",
    "IbLiquidity",
    "IbOcaType",
    "IbOptionRight",
    "IbOrderOpenClose",
    "IbOrderOrigin",
    "IbOrderStatus",
    "IbOrderType",
    "IbRealtimeBarSize",
    "IbRealtimeWhatToShow",
    "IbReferencePriceType",
    "IbRiskAversion",
    "IbRule80A",
    "IbSecurityType",
    "IbShortSaleSlot",
    "IbTickType",
    "IbTimeInForce",
    "IbTradingHours",
    "IbTriggerMethod",
    "IbTwapStrategyType",
    "IbVolatilityType",
    "InteractiveBrokersDataClientConfig",
    "InteractiveBrokersDataClientFactory",
    "InteractiveBrokersExecutionClientConfig",
    "InteractiveBrokersExecutionClientFactory",
    "InteractiveBrokersInstrumentProvider",
    "InteractiveBrokersInstrumentProviderConfig",
    "InteractiveBrokersSubscriptionIdle",
    "MarketDataType",
    "SymbologyMethod",
    "TradingMode",
]

globals().update(adapter_exports("interactive_brokers", __all__))

VENUE = "IBKR"
SILO = "US_IBKR"
SETTLEMENT_CYCLE = "T+1"
SUPPORTS_POST_ONLY = False
