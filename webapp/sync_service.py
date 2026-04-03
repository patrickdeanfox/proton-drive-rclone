"""
Sync service abstraction layer.

This module provides a provider-agnostic interface for cloud sync operations.
The current implementation delegates to rclone, but the interface is designed
to support future backends (direct API integrations, alternative sync tools).

Usage:
    service = RcloneSyncService(config)
    service.list_dir("remote:", "/path")
    service.sync("/local/path", "remote:/path", direction="bisync")

Future providers would implement the same SyncService interface.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SyncDirection(Enum):
    BISYNC = "bisync"
    PUSH = "push"
    PULL = "pull"


class ProviderType(Enum):
    PROTON_DRIVE = "protondrive"
    # Future providers:
    # GOOGLE_DRIVE = "drive"
    # S3 = "s3"
    # ONEDRIVE = "onedrive"
    # BACKBLAZE_B2 = "b2"


@dataclass
class SyncResult:
    success: bool
    message: str = ""
    exit_code: int = 0
    files_transferred: int = 0
    bytes_transferred: int = 0


@dataclass
class FileEntry:
    name: str
    path: str
    is_dir: bool
    size: int = 0
    modified: str = ""


@dataclass
class ProviderConfig:
    """Configuration for a single cloud provider."""
    name: str
    provider_type: ProviderType
    remote_name: str  # rclone remote name
    enabled: bool = True
    bandwidth_limit: str = ""
    max_transfers: int = 4
    max_checkers: int = 8
    extra_flags: list = field(default_factory=list)


@dataclass
class SyncConfig:
    """Configuration for a single sync job."""
    id: str
    name: str
    local_path: str
    remote_path: str
    provider: str  # provider name reference
    direction: SyncDirection = SyncDirection.BISYNC
    exclude_patterns: str = ""
    enabled: bool = True


class SyncService(ABC):
    """Abstract interface for sync operations.

    All cloud provider integrations should implement this interface.
    This allows swapping rclone for a direct API integration or
    adding providers not supported by rclone.
    """

    @abstractmethod
    def test_connection(self, remote_name: str) -> SyncResult:
        """Test connectivity to a remote."""
        ...

    @abstractmethod
    def list_dir(self, remote_name: str, path: str) -> list[FileEntry]:
        """List files in a remote directory."""
        ...

    @abstractmethod
    def list_dir_tree(self, remote_name: str, path: str) -> list[FileEntry]:
        """List only directories (for folder picker)."""
        ...

    @abstractmethod
    def sync(
        self,
        local_path: str,
        remote_name: str,
        remote_path: str,
        direction: SyncDirection,
        config: dict,
    ) -> SyncResult:
        """Execute a sync operation."""
        ...

    @abstractmethod
    def get_quota(self, remote_name: str) -> Optional[dict]:
        """Get storage quota information."""
        ...

    @abstractmethod
    def list_remotes(self) -> list[dict]:
        """List all configured remotes."""
        ...


# Feature flags — controls which features are active.
# In the future, these can be loaded from config.env or a dedicated flags file.
FEATURE_FLAGS = {
    "multi_cloud": False,        # Phase 2: multiple cloud providers
    "ai_search": False,          # Phase 3: local LLM search
    "duplicate_detection": False, # Phase 3: enhanced duplicate detection
    "face_recognition": False,   # Phase 4: local facial recognition
    "audit_logs": False,         # Phase 5: operation audit logging
}


def is_feature_enabled(flag_name: str) -> bool:
    """Check if a feature flag is enabled."""
    return FEATURE_FLAGS.get(flag_name, False)
