from django.core.serializers.json import DjangoJSONEncoder

import shopify


class ShopifyDjangoJSONEncoder(DjangoJSONEncoder):
    """As per: https://docs.djangoproject.com/en/1.6/topics/serialization/,
    this is a special encoder that handles lazily evaluated strings."""

    def default(self, obj):
        if isinstance(obj, shopify.ShopifyResource) and getattr(obj, 'attributes'):
            return obj.attributes
        return super(ShopifyDjangoJSONEncoder, self).default(obj)