"""Protocol-based account registration primitives."""

from .models import (
    ProtocolCall,
    ProtocolRegistrationConfig,
    ProtocolRegistrationRequest,
    ProtocolRegistrationResult,
    ProtocolResponse,
    RegistrationMode,
    SentinelData,
)
from .otp import CallableOtpProvider, MailboxOtpProvider, OtpProvider, StaticOtpProvider
from .sentinel import (
    AutoSentinelProvider,
    HttpSentinelProvider,
    SentinelProvider,
    StaticSentinelProvider,
)
from .sentinel_quickjs import QuickJsSentinelProvider
from .service import PROTOCOL_ENDPOINTS, ProtocolRegistrationError, ProtocolRegistrationService
from .transport import CurlCffiTransport, DefaultProtocolTransport, FakeTransport, ProtocolTransport

__all__ = [
    "AutoSentinelProvider",
    "CallableOtpProvider",
    "CurlCffiTransport",
    "DefaultProtocolTransport",
    "FakeTransport",
    "HttpSentinelProvider",
    "MailboxOtpProvider",
    "OtpProvider",
    "ProtocolCall",
    "ProtocolRegistrationConfig",
    "PROTOCOL_ENDPOINTS",
    "ProtocolRegistrationError",
    "ProtocolRegistrationRequest",
    "ProtocolRegistrationResult",
    "ProtocolResponse",
    "ProtocolRegistrationService",
    "ProtocolTransport",
    "QuickJsSentinelProvider",
    "RegistrationMode",
    "SentinelData",
    "SentinelProvider",
    "StaticOtpProvider",
    "StaticSentinelProvider",
]
