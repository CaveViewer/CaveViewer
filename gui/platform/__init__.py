from .factory import get_platform_adapter, get_splash_platform_adapter
from .linux import LinuxSplashPlatformAdapter
from .macos import MacOSSplashPlatformAdapter
from .windows import WindowsSplashPlatformAdapter

__all__ = [
	"get_platform_adapter",
	"get_splash_platform_adapter",
	"MacOSSplashPlatformAdapter",
	"WindowsSplashPlatformAdapter",
	"LinuxSplashPlatformAdapter",
]
