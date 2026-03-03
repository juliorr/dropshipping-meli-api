# Import all models here in dependency order so SQLAlchemy can resolve
# string-based relationship references at mapper configuration time.
from app.models.order import Order  # noqa: F401 — must be before MeliListing
from app.models.listing import MeliListing  # noqa: F401
