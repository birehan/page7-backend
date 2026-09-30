"""Import every ORM module so Base.metadata resolves cross-feature FKs.

SQLAlchemy string foreign keys (e.g. posts.approved_review_link_id → review_links.id)
only resolve when the target table has been registered on the shared MetaData. The API
process gets this for free via router imports; the worker and one-off scripts must
import models explicitly — same pattern as alembic/env.py.
"""

from __future__ import annotations

from app.features.analytics import models as _analytics_models  # noqa: F401
from app.features.audit import models as _audit_models  # noqa: F401
from app.features.auth import models as _auth_models  # noqa: F401
from app.features.billing import models as _billing_models  # noqa: F401
from app.features.brand_research import models as _brand_research_models  # noqa: F401
from app.features.brands import models as _brands_models  # noqa: F401
from app.features.content_ai import models as _content_ai_models  # noqa: F401
from app.features.cultural_events import models as _cultural_events_models  # noqa: F401
from app.features.inbox import models as _inbox_models  # noqa: F401
from app.features.media import models as _media_models  # noqa: F401
from app.features.notifications import models as _notifications_models  # noqa: F401
from app.features.organizations import models as _organizations_models  # noqa: F401
from app.features.posts import models as _posts_models  # noqa: F401
from app.features.publishing import models as _publishing_models  # noqa: F401
from app.features.review_links import models as _review_links_models  # noqa: F401
from app.features.social_accounts import models as _social_accounts_models  # noqa: F401
from app.features.strategy import models as _strategy_models  # noqa: F401
from app.features.team import models as _team_models  # noqa: F401
from app.features.visuals import models as _visuals_models  # noqa: F401
from app.infrastructure.idempotency import models as _idempotency_models  # noqa: F401
from app.infrastructure.ratelimit import models as _ratelimit_models  # noqa: F401
from app.jobs import models as _jobs_models  # noqa: F401
from app.sse import models as _sse_models  # noqa: F401
