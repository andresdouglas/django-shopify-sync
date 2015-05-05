import math
import logging

from django.db import models
from django.core.serializers.json import DjangoJSONEncoder
from owned_models.models import UserOwnedModel, UserOwnedManager

from .. import SHOPIFY_API_PAGE_LIMIT


logger = logging.getLogger(__name__)


def get_shopify_pagination(total_count):
    """
    Get the appropriate pagination to use with Shopify's API given the total number of records.
    """
    return 1, int(math.ceil(float(total_count) / float(SHOPIFY_API_PAGE_LIMIT))), SHOPIFY_API_PAGE_LIMIT


class ShopifyResourceManager(UserOwnedManager):
    """
    Base class for managing Shopify resource models.
    """

    def sync_one(self, user, shopify_resource):
        """
        Given a Shopify resource object, synchronise it locally so that we have an up-to-date version in the local
        database. Returns the created or updated local model.
        """
        # Synchronise any related model field.
        for related_field_name in self.model.get_related_field_names():
            related_shopify_resource = getattr(shopify_resource, related_field_name)
            related_model = getattr(self.model, related_field_name).field.rel.to
            related_model.objects.sync_one(user, related_shopify_resource)

        # Synchronise instance.
        instance, created = self.update_or_create(user, id = shopify_resource.id, defaults = self.model.get_defaults(shopify_resource))

        # Synchronise any child fields.
        for child_field, child_model in self.model.get_child_fields().items():
            child_shopify_resources = getattr(shopify_resource, child_field)
            child_model.objects.sync_many(user, child_shopify_resources, parent_shopify_resource = shopify_resource)

        return instance

    def sync_many(self, user, shopify_resources, parent_shopify_resource = None):
        """
        Given an array of Shopify resource objects, synchronise all of them locally so that we have up-to-date versions
        in the local database, Returns an array of the created or updated local models.
        """
        instances = []
        for shopify_resource in shopify_resources:
            # If needed, ensure the parent ID is stored on the resource before synchronising it.
            if self.model.parent_field is not None and parent_shopify_resource is not None:
                setattr(shopify_resource, self.model.parent_field, getattr(parent_shopify_resource, 'id'))
            instance = self.sync_one(user, shopify_resource)
            instances.append(instance)
        return instances
    
    def sync_all(self, user, **kwargs):
        """
        Synchronised all Shopify resources matched by the given **kwargs filter to our local database.
        Returns the synchronised local model instances.
        """
        shopify_resources = self.fetch_all(user, **kwargs)
        return self.sync_many(user, shopify_resources)
    
    def fetch_all(self, user, **kwargs):
        """
        Generator function, which fetches all Shopify resources matched by the given **kwargs filter.
        """
        with user.session:
            total_count = self.model.shopify_resource_class.count(**kwargs)
            current_page, total_pages, kwargs['limit'] = get_shopify_pagination(total_count)
                        
            while current_page <= total_pages:
                kwargs['page'] = current_page
                shopify_resources = self.model.shopify_resource_class.find(**kwargs)
                for shopify_resource in shopify_resources:
                    yield shopify_resource
                current_page += 1

    def push_one(self, user, instance):
        """
        Push a local model instance to Shopify, creating or updating in the process.

        The instance parameter can be either a ShopifyResourceModel, or an already-prepared ShopifyResource.

        Returns the locally synchronised model on success.
        """
        # Ensure we have a ShopifyResource prepared.
        if hasattr(instance, 'to_shopify_resource'):
            shopify_resource = instance.to_shopify_resource()
        else:
            shopify_resource = instance

        # Save the Shopify resource.
        with user.session:
            if not shopify_resource.save():
                message = '[User]: {0} [Shopify API Errors]: {1}'.format(user.id, ', '.join(shopify_resource.errors.full_messages()))
                logger.error(message)
                raise Exception(message)
        return self.sync_one(user, shopify_resource)

    def push_many(self, user, instances):
        """
        Push a list of local model instances to Shopify, creating or updating in the process.

        Returns the list of locally synchronised models on success.
        """
        synchronised_instances = []
        for instance in instances:
            synchronised_instance = self.push_one(user, instance)
            synchronised_instances.append(synchronised_instance)
        return synchronised_instances

    class Meta:
        abstract = True


class ShopifyResourceModel(UserOwnedModel):
    """
    Base class for local Model objects that are to be synchronised with Shopify.
    """

    shopify_resource_class = None
    parent_field = None
    related_fields = []
    child_fields = {}

    objects = ShopifyResourceManager()

    json_encoder = DjangoJSONEncoder()

    @classmethod
    def get_defaults(cls, shopify_resource):
        """
        Get a hash of attribute: values that can be used to update or create a local instance of the given Shopify
        resource.
        """
        defaults = {}

        # Set simple attributes that we simply need to copy across.
        for field in cls.get_default_fields():
            if hasattr(shopify_resource, field):
                defaults[field] = getattr(shopify_resource, field)

        # Set ID values for foreign key references.
        for related_field in cls.get_related_field_names():
            if hasattr(shopify_resource, related_field):
                defaults[related_field + '_id'] = getattr(getattr(shopify_resource, related_field), 'id')

        return defaults

    @classmethod
    def get_default_fields(cls):
        """
        Get a list of field names that should be copied directly from a Shopify resource model when building the
        defaults hash.
        """
        default_fields_excluded = cls.get_default_fields_excluded()
        return [field.name for field in cls._meta.concrete_fields if field.name not in default_fields_excluded]

    @classmethod
    def get_default_fields_excluded(cls):
        """
        Get a list of field names to be excluded when copying directly from a Shopify resource model and building
        a defaults hash.
        """
        return ['user'] + cls.get_parent_field_names() + cls.get_related_field_names() + cls.get_child_fields().keys()

    @classmethod
    def get_parent_field_names(cls):
        """
        Get a list of the names of parent fields for the current model.
        """
        if cls.parent_field is None:
            return []
        return [cls.parent_field]

    @classmethod
    def get_related_field_names(cls):
        """
        Get a list of the names of related fields for the current model.
        """
        return cls.related_fields

    @classmethod
    def get_child_fields(cls):
        """
        Get a list of child fields for the current model, in a "hash" format with the name of the field as the key
        and the related child model as the value.
        """
        return cls.child_fields

    @classmethod
    def shopify_resource_from_json(cls, json):
        """
        Return an instance of the Shopify Resource model for this model, built recursively using the given JSON object.
        """
        instance = cls.shopify_resource_class()

        # Recursively instantiate any child attributes.
        for child_field, child_model in cls.get_child_fields().items():
            if child_field in json:
                json[child_field] = [child_model.shopify_resource_from_json(child_field_json) for child_field_json in json[child_field]]

        instance.attributes = json
        return instance

    def to_shopify_resource(self):
        """
        Convert this ShopifyResource model instance to its equivalent ShopifyResource.
        """
        instance = self.shopify_resource_class()

        # Copy across attributes.
        for default_field in self.get_default_fields():
            if hasattr(self, default_field):
                attribute = getattr(self, default_field)
                # If the attribute is itself a ShopifyResourceModel, ignore it.
                # The relevant resource will be linked through a '_id' parameter.
                if not isinstance(attribute, ShopifyResourceModel):
                    try:
                        attribute_encoded = self.json_encoder.default(attribute)
                    except TypeError:
                        attribute_encoded = attribute
                    finally:
                        setattr(instance, default_field, attribute_encoded)

        # Recursively instantiate any child attributes.
        for child_field, child_model in self.get_child_fields().items():
            if hasattr(self, child_field):
                setattr(instance, child_field, [child.to_shopify_resource() for child in getattr(self, child_field)])

        return instance

    def to_json(self):
        """
        Convert this ShopifyResource model instance to a "JSON" (simple Python) object.
        """
        return self.to_shopify_resource().attributes

    class Meta:
        abstract = True


class ShopifyDatedResourceModel(ShopifyResourceModel):
    """
    Extends ShopifyResourceModel by adding two common fields for Shopify resources - `created_at` and `updated_at`.
    """

    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        abstract = True
