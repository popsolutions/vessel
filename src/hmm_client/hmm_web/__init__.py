"""HMM proprietary web-API client.

The Huawei E9000 HMM ships a non-Redfish web GUI that's the only
surface for several operations: SMM/switch CPLD upgrades, vNIC profile
mgmt, easyLink wiring, MAC/UUID pools. This package wraps the
dispatcher pattern (POST /<x>handler.php with ``actiontype=<verb>``,
form-encoded request, XML response) into a typed Python client.

See ``docs/hmm-api/`` (TBD) and the captures under ``discovery/sweep/``
for the protocol details.
"""

from .client import HMMWebClient, HMMWebError, HMMWebResult
from .firmware import FirmwareModule, UpgradeStatus, UpgradeTarget
from .inventory import ComponentVersion, InventoryModule

__all__ = (
    "HMMWebClient",
    "HMMWebError",
    "HMMWebResult",
    "InventoryModule",
    "ComponentVersion",
    "FirmwareModule",
    "UpgradeStatus",
    "UpgradeTarget",
)
