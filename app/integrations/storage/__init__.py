from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import Depends

from app.core.config import Environment, Settings, get_settings
from app.integrations.storage.local import LocalStorage
from app.integrations.storage.ports import ObjectStorage
from app.integrations.storage.r2 import R2Storage

_LOCAL_ENVS = (Environment.DEVELOPMENT, Environment.TESTING)


def get_object_storage(settings: Annotated[Settings, Depends(get_settings)]) -> ObjectStorage:
    """architecture/06 §2: which adapter answers is a configuration value."""
    resolve_to_local = settings.storage.provider == "local" or (
        settings.storage.provider == "auto" and settings.app_env in _LOCAL_ENVS
    )
    if resolve_to_local:
        return LocalStorage(
            root=Path(settings.storage.local_root),
            public_base_url=settings.storage.public_base_url,
            public_bucket=settings.storage.public_bucket,
            private_bucket=settings.storage.private_bucket,
        )

    account_id = settings.storage.r2_account_id
    access_key = settings.storage.r2_access_key_id
    secret = settings.storage.r2_secret_access_key
    if not account_id or access_key is None or secret is None:
        raise RuntimeError(
            "STORAGE__R2_ACCOUNT_ID / STORAGE__R2_ACCESS_KEY_ID / "
            "STORAGE__R2_SECRET_ACCESS_KEY must be configured for R2"
        )
    return R2Storage(
        account_id=account_id,
        access_key_id=access_key.get_secret_value(),
        secret_access_key=secret.get_secret_value(),
        public_bucket=settings.storage.public_bucket,
        public_base_url=settings.storage.public_base_url,
        private_bucket=settings.storage.private_bucket,
    )
