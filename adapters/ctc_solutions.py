"""City Telecommunication Centre / CTC Solutions (WooCommerce, whole shillings, "KSh8,000 Excl. Tax").

SKUs are usually empty, so the model number is read from the product name.
SiteGround bot protection sometimes refuses requests (HTTP 403); that is
reported as "didn't respond" and never worked around.
"""

from adapters.woocommerce import WooCommerceAdapter


class CTCSolutionsAdapter(WooCommerceAdapter):
    pass
