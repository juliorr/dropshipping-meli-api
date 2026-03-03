# Import all models here in dependency order so SQLAlchemy can resolve
# string-based relationship references at mapper configuration time.
from app.models.order import Order  # noqa: F401 — must be before MeliListing
from app.models.listing import MeliListing  # noqa: F401
from app.models.product_image import ProductImage  # noqa: F401
from app.models.product import Product  # noqa: F401
from app.models.product_variation import ProductVariation  # noqa: F401
