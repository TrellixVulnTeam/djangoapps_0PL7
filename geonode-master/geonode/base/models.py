# -*- coding: utf-8 -*-
#########################################################################
#
# Copyright (C) 2016 OSGeo
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
#########################################################################

import os
import re
import math
import uuid
import logging
import traceback

from django.db import models
from django.conf import settings
from django.core import serializers
from django.utils.functional import cached_property
from django.utils.html import escape
from django.utils.timezone import now
from django.db.models import Q, signals
from django.contrib.auth.models import Group
from django.core.files.base import ContentFile
from django.contrib.auth import get_user_model
from django.contrib.gis.geos import GEOSGeometry, Polygon, Point
from django.contrib.gis.db.models import PolygonField
from django.core.exceptions import ValidationError
from django.utils.translation import ugettext_lazy as _
from django.contrib.contenttypes.models import ContentType
from django.contrib.staticfiles.templatetags import staticfiles
from django.core.files.storage import default_storage as storage

from mptt.models import MPTTModel, TreeForeignKey

from PIL import Image
from io import BytesIO
from resizeimage import resizeimage

from imagekit.models import ImageSpecField
from imagekit.processors import ResizeToFill

from polymorphic.models import PolymorphicModel
from polymorphic.managers import PolymorphicManager
from pinax.ratings.models import OverallRating

from taggit.models import TagBase, ItemBase
from taggit.managers import TaggableManager, _TaggableManager

from guardian.shortcuts import get_anonymous_user, get_objects_for_user
from treebeard.mp_tree import MP_Node, MP_NodeQuerySet, MP_NodeManager

from geonode.singleton import SingletonModel
from geonode.base.enumerations import (
    LINK_TYPES,
    ALL_LANGUAGES,
    HIERARCHY_LEVELS,
    UPDATE_FREQUENCIES,
    DEFAULT_SUPPLEMENTAL_INFORMATION)
from geonode.base.bbox_utils import BBOXHelper
from geonode.utils import (
    add_url_params,
    bbox_to_wkt)
from geonode.groups.models import GroupProfile
from geonode.security.utils import get_visible_resources
from geonode.security.models import PermissionLevelMixin

from geonode.notifications_helper import (
    send_notification,
    get_notification_recipients)
from geonode.people.enumerations import ROLE_VALUES
from geonode.base.thumb_utils import (
    thumb_path,
    remove_thumbs)

from pyproj import transform, Proj

from urllib.parse import urlparse, urlsplit, urljoin
from imagekit.cachefiles.backends import Simple

logger = logging.getLogger(__name__)


class ContactRole(models.Model):
    """
    ContactRole is an intermediate model to bind Profiles as Contacts to Resources and apply roles.
    """
    resource = models.ForeignKey('ResourceBase', blank=False, null=False, on_delete=models.CASCADE)
    contact = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    role = models.CharField(
        choices=ROLE_VALUES,
        max_length=255,
        help_text=_(
            'function performed by the responsible '
            'party'))

    def clean(self):
        """
        Make sure there is only one poc and author per resource
        """

        if not hasattr(self, 'resource'):
            # The ModelForm will already raise a Validation error for a missing resource.
            # Re-raising an empty error here ensures the rest of this method isn't
            # executed.
            raise ValidationError('')

        if (self.role == self.resource.poc) or (
                self.role == self.resource.metadata_author):
            contacts = self.resource.contacts.filter(
                contactrole__role=self.role)
            if contacts.count() == 1:
                # only allow this if we are updating the same contact
                if self.contact != contacts.get():
                    raise ValidationError(
                        'There can be only one %s for a given resource' %
                        self.role)
        if self.contact is None:
            # verify that any unbound contact is only associated to one
            # resource
            bounds = ContactRole.objects.filter(contact=self.contact).count()
            if bounds > 1:
                raise ValidationError(
                    'There can be one and only one resource linked to an unbound contact' %
                    self.role)
            elif bounds == 1:
                # verify that if there was one already, it corresponds to this
                # instance
                if ContactRole.objects.filter(
                        contact=self.contact).get().id != self.id:
                    raise ValidationError(
                        'There can be one and only one resource linked to an unbound contact' %
                        self.role)

    class Meta:
        unique_together = (("contact", "resource", "role"),)


class TopicCategory(models.Model):
    """
    Metadata about high-level geographic data thematic classification.
    It should reflect a list of codes from TC211
    See: http://www.isotc211.org/2005/resources/Codelist/gmxCodelists.xml
    <CodeListDictionary gml:id="MD_MD_TopicCategoryCode">
    """
    identifier = models.CharField(max_length=255, default='location')
    description = models.TextField(default='')
    gn_description = models.TextField(
        'GeoNode description', default='', null=True)
    is_choice = models.BooleanField(default=True)
    fa_class = models.CharField(max_length=64, default='fa-times')

    def __str__(self):
        return self.gn_description

    class Meta:
        ordering = ("identifier",)
        verbose_name_plural = 'Metadata Topic Categories'


class SpatialRepresentationType(models.Model):
    """
    Metadata information about the spatial representation type.
    It should reflect a list of codes from TC211
    See: http://www.isotc211.org/2005/resources/Codelist/gmxCodelists.xml
    <CodeListDictionary gml:id="MD_SpatialRepresentationTypeCode">
    """
    identifier = models.CharField(max_length=255, editable=False)
    description = models.CharField(max_length=255, editable=False)
    gn_description = models.CharField('GeoNode description', max_length=255)
    is_choice = models.BooleanField(default=True)

    def __str__(self):
        return "{0}".format(self.gn_description)

    class Meta:
        ordering = ("identifier",)
        verbose_name_plural = 'Metadata Spatial Representation Types'


class Region(MPTTModel):
    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=255)
    parent = TreeForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='children')

    # Save bbox values in the database.
    # This is useful for spatial searches and for generating thumbnail images
    # and metadata records.
    bbox_x0 = models.DecimalField(
        max_digits=30,
        decimal_places=15,
        blank=True,
        null=True)
    bbox_x1 = models.DecimalField(
        max_digits=30,
        decimal_places=15,
        blank=True,
        null=True)
    bbox_y0 = models.DecimalField(
        max_digits=30,
        decimal_places=15,
        blank=True,
        null=True)
    bbox_y1 = models.DecimalField(
        max_digits=30,
        decimal_places=15,
        blank=True,
        null=True)
    srid = models.CharField(
        max_length=30,
        blank=False,
        null=False,
        default='EPSG:4326')

    def __str__(self):
        return "{0}".format(self.name)

    @property
    def bbox(self):
        """BBOX is in the format: [x0,x1,y0,y1]."""
        return [
            self.bbox_x0,
            self.bbox_x1,
            self.bbox_y0,
            self.bbox_y1,
            self.srid]

    @property
    def bbox_string(self):
        """BBOX is in the format: [x0,y0,x1,y1]."""
        return ",".join([str(self.bbox_x0), str(self.bbox_y0),
                         str(self.bbox_x1), str(self.bbox_y1)])

    @property
    def geographic_bounding_box(self):
        """BBOX is in the format: [x0,x1,y0,y1]."""
        return bbox_to_wkt(
            self.bbox_x0,
            self.bbox_x1,
            self.bbox_y0,
            self.bbox_y1,
            srid=self.srid)

    class Meta:
        ordering = ("name",)
        verbose_name_plural = 'Metadata Regions'

    class MPTTMeta:
        order_insertion_by = ['name']


class RestrictionCodeType(models.Model):
    """
    Metadata information about the spatial representation type.
    It should reflect a list of codes from TC211
    See: http://www.isotc211.org/2005/resources/Codelist/gmxCodelists.xml
    <CodeListDictionary gml:id="MD_RestrictionCode">
    """
    identifier = models.CharField(max_length=255, editable=False)
    description = models.TextField(max_length=255, editable=False)
    gn_description = models.TextField('GeoNode description', max_length=255)
    is_choice = models.BooleanField(default=True)

    def __str__(self):
        return "{0}".format(self.gn_description)

    class Meta:
        ordering = ("identifier",)
        verbose_name_plural = 'Metadata Restriction Code Types'


class License(models.Model):
    identifier = models.CharField(max_length=255, editable=False)
    name = models.CharField(max_length=255)
    abbreviation = models.CharField(max_length=20, null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    url = models.URLField(max_length=2000, null=True, blank=True)
    license_text = models.TextField(null=True, blank=True)

    def __str__(self):
        return "{0}".format(self.name)

    @property
    def name_long(self):
        if self.abbreviation is None or len(self.abbreviation) == 0:
            return self.name
        else:
            return self.name + " (" + self.abbreviation + ")"

    @property
    def description_bullets(self):
        if self.description is None or len(self.description) == 0:
            return ""
        else:
            bullets = []
            lines = self.description.split("\n")
            for line in lines:
                bullets.append("+ " + line)
            return bullets

    class Meta:
        ordering = ("name",)
        verbose_name_plural = 'Licenses'


class HierarchicalKeywordQuerySet(MP_NodeQuerySet):
    """QuerySet to automatically create a root node if `depth` not given."""

    def create(self, **kwargs):
        if 'depth' not in kwargs:
            return self.model.add_root(**kwargs)
        return super(HierarchicalKeywordQuerySet, self).create(**kwargs)


class HierarchicalKeywordManager(MP_NodeManager):

    def get_queryset(self):
        return HierarchicalKeywordQuerySet(self.model).order_by('path')


class HierarchicalKeyword(TagBase, MP_Node):
    node_order_by = ['name']

    objects = HierarchicalKeywordManager()

    @classmethod
    def dump_bulk_tree(cls, user, parent=None, keep_ids=True, type=None):
        """Dumps a tree branch to a python data structure."""
        user = user or get_anonymous_user()
        ctype_filter = [type, ] if type else ['layer', 'map', 'document']
        qset = cls._get_serializable_model().get_tree(parent)
        if settings.SKIP_PERMS_FILTER:
            resources = ResourceBase.objects.all()
        else:
            resources = get_objects_for_user(
                user,
                'base.view_resourcebase'
            )
        resources = resources.filter(
            polymorphic_ctype__model__in=ctype_filter,
        )
        resources = get_visible_resources(
            resources,
            user,
            admin_approval_required=settings.ADMIN_MODERATE_UPLOADS,
            unpublished_not_visible=settings.RESOURCE_PUBLISHING,
            private_groups_not_visibile=settings.GROUP_PRIVATE_RESOURCES)
        ret, lnk = [], {}
        try:
            for pyobj in qset.order_by('name'):
                serobj = serializers.serialize('python', [pyobj])[0]
                # django's serializer stores the attributes in 'fields'
                fields = serobj['fields']
                depth = fields['depth'] or 1
                tags_count = 0
                try:
                    tags_count = TaggedContentItem.objects.filter(
                        content_object__in=resources,
                        tag=HierarchicalKeyword.objects.get(slug=fields['slug'])).count()
                except Exception:
                    pass
                if tags_count > 0:
                    fields['text'] = fields['name']
                    fields['href'] = fields['slug']
                    fields['tags'] = [tags_count]
                    del fields['name']
                    del fields['slug']
                    del fields['path']
                    del fields['numchild']
                    del fields['depth']
                    if 'id' in fields:
                        # this happens immediately after a load_bulk
                        del fields['id']
                    newobj = {}
                    for field in fields:
                        newobj[field] = fields[field]
                    if keep_ids:
                        newobj['id'] = serobj['pk']

                    if (not parent and depth == 1) or \
                            (parent and depth == parent.depth):
                        ret.append(newobj)
                    else:
                        parentobj = pyobj.get_parent()
                        parentser = lnk[parentobj.pk]
                        if 'nodes' not in parentser:
                            parentser['nodes'] = []
                        parentser['nodes'].append(newobj)
                    lnk[pyobj.pk] = newobj
        except Exception:
            pass
        return ret


class TaggedContentItem(ItemBase):
    content_object = models.ForeignKey('ResourceBase', on_delete=models.CASCADE)
    tag = models.ForeignKey('HierarchicalKeyword', related_name='keywords', on_delete=models.CASCADE)

    # see https://github.com/alex/django-taggit/issues/101
    @classmethod
    def tags_for(cls, model, instance=None):
        if instance is not None:
            return cls.tag_model().objects.filter(**{
                '%s__content_object' % cls.tag_relname(): instance
            })
        return cls.tag_model().objects.filter(**{
            '%s__content_object__isnull' % cls.tag_relname(): False
        }).distinct()


class _HierarchicalTagManager(_TaggableManager):
    def add(self, *tags):
        str_tags = set([
            t
            for t in tags
            if not isinstance(t, self.through.tag_model())
        ])
        tag_objs = set(tags) - str_tags
        # If str_tags has 0 elements Django actually optimizes that to not do a
        # query.  Malcolm is very smart.
        existing = self.through.tag_model().objects.filter(
            name__in=str_tags
        )
        tag_objs.update(existing)
        for new_tag in str_tags - set(t.name for t in existing):
            if new_tag:
                new_tag = escape(new_tag)
                tag_objs.add(HierarchicalKeyword.add_root(name=new_tag))

        for tag in tag_objs:
            try:
                self.through.objects.get_or_create(
                    tag=tag, **self._lookup_kwargs())
            except Exception as e:
                logger.exception(e)


class Thesaurus(models.Model):
    """
    Loadable thesaurus containing keywords in different languages
    """
    identifier = models.CharField(
        max_length=255,
        null=False,
        blank=False,
        unique=True)

    # read from the RDF file
    title = models.CharField(max_length=255, null=False, blank=False)
    # read from the RDF file
    date = models.CharField(max_length=20, default='')
    # read from the RDF file
    description = models.TextField(max_length=255, default='')

    slug = models.CharField(max_length=64, default='')

    def __str__(self):
        return "{0}".format(self.identifier)

    class Meta:
        ordering = ("identifier",)
        verbose_name_plural = 'Thesauri'


class ThesaurusKeywordLabel(models.Model):
    """
    Loadable thesaurus containing keywords in different languages
    """

    # read from the RDF file
    lang = models.CharField(max_length=3)
    # read from the RDF file
    label = models.CharField(max_length=255)
    # note  = models.CharField(max_length=511)

    keyword = models.ForeignKey('ThesaurusKeyword', related_name='keyword', on_delete=models.CASCADE)

    def __str__(self):
        return "{0}".format(self.label)

    class Meta:
        ordering = ("keyword", "lang")
        verbose_name_plural = 'Labels'
        unique_together = (("keyword", "lang"),)


class ThesaurusKeyword(models.Model):
    """
    Loadable thesaurus containing keywords in different languages
    """
    # read from the RDF file
    about = models.CharField(max_length=255, null=True, blank=True)
    # read from the RDF file
    alt_label = models.CharField(
        max_length=255,
        default='',
        null=True,
        blank=True)

    thesaurus = models.ForeignKey('Thesaurus', related_name='thesaurus', on_delete=models.CASCADE)

    def __str__(self):
        return "{0}".format(self.alt_label)

    @property
    def labels(self):
        return ThesaurusKeywordLabel.objects.filter(keyword=self)

    class Meta:
        ordering = ("alt_label",)
        verbose_name_plural = 'Thesaurus Keywords'
        unique_together = (("thesaurus", "alt_label"),)


class ResourceBaseManager(PolymorphicManager):
    def admin_contact(self):
        # this assumes there is at least one superuser
        superusers = get_user_model().objects.filter(is_superuser=True).order_by('id')
        if superusers.count() == 0:
            raise RuntimeError(
                'GeoNode needs at least one admin/superuser set')

        return superusers[0]

    def get_queryset(self):
        return super(
            ResourceBaseManager,
            self).get_queryset().non_polymorphic()

    def polymorphic_queryset(self):
        return super(ResourceBaseManager, self).get_queryset()


class ResourceBase(PolymorphicModel, PermissionLevelMixin, ItemBase):
    """
    Base Resource Object loosely based on ISO 19115:2003
    """
    BASE_PERMISSIONS = {
        'read': ['view_resourcebase'],
        'write': [
            'change_resourcebase_metadata'
        ],
        'download': ['download_resourcebase'],
        'owner': [
            'change_resourcebase',
            'delete_resourcebase',
            'change_resourcebase_permissions',
            'publish_resourcebase'
        ]
    }

    PERMISSIONS = {}

    VALID_DATE_TYPES = [(x.lower(), _(x))
                        for x in ['Creation', 'Publication', 'Revision']]

    date_help_text = _('reference date for the cited resource')
    date_type_help_text = _('identification of when a given event occurred')
    edition_help_text = _('version of the cited resource')
    abstract_help_text = _(
        'brief narrative summary of the content of the resource(s)')
    purpose_help_text = _(
        'summary of the intentions with which the resource(s) was developed')
    maintenance_frequency_help_text = _(
        'frequency with which modifications and deletions are made to the data after '
        'it is first produced')
    keywords_help_text = _(
        'commonly used word(s) or formalised word(s) or phrase(s) used to describe the subject '
        '(space or comma-separated)')
    tkeywords_help_text = _(
        'formalised word(s) or phrase(s) from a fixed thesaurus used to describe the subject '
        '(space or comma-separated)')
    regions_help_text = _('keyword identifies a location')
    restriction_code_type_help_text = _(
        'limitation(s) placed upon the access or use of the data.')
    constraints_other_help_text = _(
        'other restrictions and legal prerequisites for accessing and using the resource or'
        ' metadata')
    license_help_text = _('license of the dataset')
    language_help_text = _('language used within the dataset')
    category_help_text = _(
        'high-level geographic data thematic classification to assist in the grouping and search of '
        'available geographic data sets.')
    spatial_representation_type_help_text = _(
        'method used to represent geographic information in the dataset.')
    temporal_extent_start_help_text = _(
        'time period covered by the content of the dataset (start)')
    temporal_extent_end_help_text = _(
        'time period covered by the content of the dataset (end)')
    data_quality_statement_help_text = _(
        'general explanation of the data producer\'s knowledge about the lineage of a'
        ' dataset')
    doi_help_text = _(
        'a DOI will be added by Admin before publication.')
    doi = models.CharField(
        _('DOI'),
        max_length=255,
        blank=True,
        null=True,
        help_text=doi_help_text)
    attribution_help_text = _(
        'authority or function assigned, as to a ruler, legislative assembly, delegate, or the like.')
    attribution = models.CharField(
        _('Attribution'),
        max_length=2048,
        blank=True,
        null=True,
        help_text=attribution_help_text)
    # internal fields
    uuid = models.CharField(max_length=36)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        related_name='owned_resource',
        verbose_name=_("Owner"),
        on_delete=models.CASCADE)
    contacts = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through='ContactRole')
    title = models.CharField(_('title'), max_length=255, help_text=_(
        'name by which the cited resource is known'))
    alternate = models.CharField(max_length=128, null=True, blank=True)
    date = models.DateTimeField(
        _('date'),
        default=now,
        help_text=date_help_text)
    date_type = models.CharField(
        _('date type'),
        max_length=255,
        choices=VALID_DATE_TYPES,
        default='publication',
        help_text=date_type_help_text)
    edition = models.CharField(
        _('edition'),
        max_length=255,
        blank=True,
        null=True,
        help_text=edition_help_text)
    abstract = models.TextField(
        _('abstract'),
        max_length=2000,
        blank=True,
        help_text=abstract_help_text)
    purpose = models.TextField(
        _('purpose'),
        max_length=500,
        null=True,
        blank=True,
        help_text=purpose_help_text)
    maintenance_frequency = models.CharField(
        _('maintenance frequency'),
        max_length=255,
        choices=UPDATE_FREQUENCIES,
        blank=True,
        null=True,
        help_text=maintenance_frequency_help_text)
    keywords = TaggableManager(
        _('keywords'),
        through=TaggedContentItem,
        blank=True,
        help_text=keywords_help_text,
        manager=_HierarchicalTagManager)
    tkeywords = models.ManyToManyField(
        ThesaurusKeyword,
        verbose_name=_('keywords'),
        null=True,
        blank=True,
        help_text=tkeywords_help_text)
    regions = models.ManyToManyField(
        Region,
        verbose_name=_('keywords region'),
        null=True,
        blank=True,
        help_text=regions_help_text)
    restriction_code_type = models.ForeignKey(
        RestrictionCodeType,
        verbose_name=_('restrictions'),
        help_text=restriction_code_type_help_text,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        limit_choices_to=Q(is_choice=True))
    constraints_other = models.TextField(
        _('restrictions other'),
        blank=True,
        null=True,
        help_text=constraints_other_help_text)
    license = models.ForeignKey(
        License,
        null=True,
        blank=True,
        verbose_name=_("License"),
        help_text=license_help_text,
        on_delete=models.SET_NULL)
    language = models.CharField(
        _('language'),
        max_length=3,
        choices=ALL_LANGUAGES,
        default='eng',
        help_text=language_help_text)
    category = models.ForeignKey(
        TopicCategory,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        limit_choices_to=Q(is_choice=True),
        help_text=category_help_text)
    spatial_representation_type = models.ForeignKey(
        SpatialRepresentationType,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        limit_choices_to=Q(is_choice=True),
        verbose_name=_("spatial representation type"),
        help_text=spatial_representation_type_help_text)

    # Section 5
    temporal_extent_start = models.DateTimeField(
        _('temporal extent start'),
        blank=True,
        null=True,
        help_text=temporal_extent_start_help_text)
    temporal_extent_end = models.DateTimeField(
        _('temporal extent end'),
        blank=True,
        null=True,
        help_text=temporal_extent_end_help_text)
    supplemental_information = models.TextField(
        _('supplemental information'),
        max_length=2000,
        default=DEFAULT_SUPPLEMENTAL_INFORMATION,
        help_text=_('any other descriptive information about the dataset'))

    # Section 8
    data_quality_statement = models.TextField(
        _('data quality statement'),
        max_length=2000,
        blank=True,
        null=True,
        help_text=data_quality_statement_help_text)
    group = models.ForeignKey(
        Group,
        null=True,
        blank=True,
        on_delete=models.SET_NULL)

    # Section 9
    # see metadata_author property definition below

    # Save bbox values in the database.
    # This is useful for spatial searches and for generating thumbnail images
    # and metadata records.
    bbox_polygon = PolygonField(null=True, blank=True)

    srid = models.CharField(
        max_length=30,
        blank=False,
        null=False,
        default='EPSG:4326')

    # CSW specific fields
    csw_typename = models.CharField(
        _('CSW typename'),
        max_length=32,
        default='gmd:MD_Metadata',
        null=False)
    csw_schema = models.CharField(
        _('CSW schema'),
        max_length=64,
        default='http://www.isotc211.org/2005/gmd',
        null=False)
    csw_mdsource = models.CharField(
        _('CSW source'),
        max_length=256,
        default='local',
        null=False)
    csw_insert_date = models.DateTimeField(
        _('CSW insert date'), auto_now_add=True, null=True)
    csw_type = models.CharField(
        _('CSW type'),
        max_length=32,
        default='dataset',
        null=False,
        choices=HIERARCHY_LEVELS)
    csw_anytext = models.TextField(_('CSW anytext'), null=True, blank=True)
    csw_wkt_geometry = models.TextField(
        _('CSW WKT geometry'),
        null=False,
        default='POLYGON((-180 -90,-180 90,180 90,180 -90,-180 -90))')

    # metadata XML specific fields
    metadata_uploaded = models.BooleanField(default=False)
    metadata_uploaded_preserve = models.BooleanField(_('Metadata uploaded preserve'), default=False)
    metadata_xml = models.TextField(
        null=True,
        default='<gmd:MD_Metadata xmlns:gmd="http://www.isotc211.org/2005/gmd"/>',
        blank=True)
    popular_count = models.IntegerField(default=0)
    share_count = models.IntegerField(default=0)
    featured = models.BooleanField(_("Featured"), default=False, help_text=_(
        'Should this resource be advertised in home page?'))
    is_published = models.BooleanField(
        _("Is Published"),
        default=True,
        help_text=_('Should this resource be published and searchable?'))
    is_approved = models.BooleanField(
        _("Approved"),
        default=True,
        help_text=_('Is this resource validated from a publisher or editor?'))

    # fields necessary for the apis
    thumbnail_url = models.TextField(_("Thumbnail url"), null=True, blank=True)
    detail_url = models.CharField(max_length=255, null=True, blank=True)
    rating = models.IntegerField(default=0, null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    last_updated = models.DateTimeField(auto_now=True, null=True, blank=True)

    # fields controlling security state
    dirty_state = models.BooleanField(
        _("Dirty State"),
        default=False,
        help_text=_('Security Rules Are Not Synched with GeoServer!'))

    users_geolimits = models.ManyToManyField(
        "UserGeoLimit",
        related_name="users_geolimits",
        null=True,
        blank=True)

    groups_geolimits = models.ManyToManyField(
        "GroupGeoLimit",
        related_name="groups_geolimits",
        null=True,
        blank=True)

    resource_type = models.CharField(
        _('Resource Type'),
        max_length=1024,
        blank=True,
        null=True)

    __is_approved = False
    __is_published = False

    objects = ResourceBaseManager()

    class Meta:
        # custom permissions,
        # add, change and delete are standard in django-guardian
        permissions = (
            # ('view_resourcebase', 'Can view resource'),
            ('change_resourcebase_permissions', 'Can change resource permissions'),
            ('download_resourcebase', 'Can download resource'),
            ('publish_resourcebase', 'Can publish resource'),
            ('change_resourcebase_metadata', 'Can change resource metadata'),
        )

    def __init__(self, *args, **kwargs):
        # Provide legacy support for bbox fields
        bbox = [kwargs.pop(key, None) for key in ('bbox_x0', 'bbox_y0', 'bbox_x1', 'bbox_y1')]
        if all(bbox):
            kwargs['bbox_polygon'] = Polygon.from_bbox(bbox)
        super(ResourceBase, self).__init__(*args, **kwargs)

    def __str__(self):
        return "{0}".format(self.title)

    def _remove_html_tags(self, attribute_str):
        try:
            pattern = re.compile('<.*?>')
            return re.sub(pattern, '', attribute_str)
        except Exception:
            return attribute_str

    @property
    def raw_abstract(self):
        return self._remove_html_tags(self.abstract)

    @property
    def raw_purpose(self):
        return self._remove_html_tags(self.purpose)

    @property
    def raw_constraints_other(self):
        return self._remove_html_tags(self.constraints_other)

    @property
    def raw_supplemental_information(self):
        return self._remove_html_tags(self.supplemental_information)

    @property
    def raw_data_quality_statement(self):
        return self._remove_html_tags(self.data_quality_statement)

    def save(self, notify=False, *args, **kwargs):
        """
        Send a notification when a resource is created or updated
        """
        if not self.resource_type and self.polymorphic_ctype and \
        self.polymorphic_ctype.model:
            self.resource_type = self.polymorphic_ctype.model.lower()

        if hasattr(self, 'class_name') and (self.pk is None or notify):
            if self.pk is None and self.title:
                # Resource Created

                notice_type_label = '%s_created' % self.class_name.lower()
                recipients = get_notification_recipients(notice_type_label, resource=self)
                send_notification(recipients, notice_type_label, {'resource': self})
            elif self.pk:
                # Resource Updated
                _notification_sent = False

                # Approval Notifications Here
                if not _notification_sent and settings.ADMIN_MODERATE_UPLOADS:
                    if not self.__is_approved and self.is_approved:
                        # Set "approved" workflow permissions
                        self.set_workflow_perms(approved=True)

                        # Send "approved" notification
                        notice_type_label = '%s_approved' % self.class_name.lower()
                        recipients = get_notification_recipients(notice_type_label, resource=self)
                        send_notification(recipients, notice_type_label, {'resource': self})
                        _notification_sent = True

                # Publishing Notifications Here
                if not _notification_sent and settings.RESOURCE_PUBLISHING:
                    if not self.__is_published and self.is_published:
                        # Set "published" workflow permissions
                        self.set_workflow_perms(published=True)

                        # Send "published" notification
                        notice_type_label = '%s_published' % self.class_name.lower()
                        recipients = get_notification_recipients(notice_type_label, resource=self)
                        send_notification(recipients, notice_type_label, {'resource': self})
                        _notification_sent = True

                # Updated Notifications Here
                if not _notification_sent:
                    notice_type_label = '%s_updated' % self.class_name.lower()
                    recipients = get_notification_recipients(notice_type_label, resource=self)
                    send_notification(recipients, notice_type_label, {'resource': self})

        super(ResourceBase, self).save(*args, **kwargs)
        self.__is_approved = self.is_approved
        self.__is_published = self.is_published

    def delete(self, notify=True, *args, **kwargs):
        """
        Send a notification when a layer, map or document is deleted
        """
        if hasattr(self, 'class_name') and notify:
            notice_type_label = '%s_deleted' % self.class_name.lower()
            recipients = get_notification_recipients(notice_type_label, resource=self)
            send_notification(recipients, notice_type_label, {'resource': self})

        super(ResourceBase, self).delete(*args, **kwargs)

    def get_upload_session(self):
        raise NotImplementedError()

    @property
    def site_url(self):
        return settings.SITEURL

    @property
    def creator(self):
        return self.owner.get_full_name() or self.owner.username

    @property
    def organizationname(self):
        return self.owner.organization

    @property
    def restriction_code(self):
        return self.restriction_code_type.gn_description

    @property
    def publisher(self):
        return self.poc.get_full_name() or self.poc.username

    @property
    def contributor(self):
        return self.metadata_author.get_full_name() or self.metadata_author.username

    @property
    def topiccategory(self):
        return self.category.identifier

    @property
    def csw_crs(self):
        return self.srid

    @property
    def group_name(self):
        if self.group:
            return str(self.group).encode("utf-8", "replace")
        return None

    @property
    def bbox(self):
        """BBOX is in the format: [x0, x1, y0, y1, srid]."""
        if self.bbox_polygon:
            bbox = self.bbox_polygon
            match = re.match(r'^(EPSG:)?(?P<srid>\d{4,6})$', self.srid)
            srid = int(match.group('srid'))
            if bbox.srid is not None and bbox.srid != srid:
                try:
                    bbox = bbox.transform(srid, clone=True)
                except Exception:
                    bbox.srid = srid
            bbox = BBOXHelper(bbox.extent)
            return [bbox.xmin, bbox.xmax, bbox.ymin, bbox.ymax, "EPSG:{}".format(srid)]
        bbox = BBOXHelper.from_xy([-180, 180, -90, 90])
        return [bbox.xmin, bbox.xmax, bbox.ymin, bbox.ymax, "EPSG:4326"]

    @property
    def ll_bbox(self):
        """BBOX is in the format [x0, x1, y0, y1, "EPSG:srid"]. Provides backwards
        compatibility after transition to polygons."""
        if self.bbox_polygon:
            bbox = self.bbox_polygon
            if bbox.srid is not None and bbox.srid != 4326:
                bbox = bbox.transform(4326, clone=True)

            bbox = BBOXHelper(bbox.extent)
            return [bbox.xmin, bbox.xmax, bbox.ymin, bbox.ymax, "EPSG:4326"]
        bbox = BBOXHelper.from_xy([-180, 180, -90, 90])
        return [bbox.xmin, bbox.xmax, bbox.ymin, bbox.ymax, "EPSG:4326"]

    @property
    def ll_bbox_string(self):
        """WGS84 BBOX is in the format: [x0,y0,x1,y1]."""
        if self.bbox_polygon:
            bbox = BBOXHelper.from_xy(self.ll_bbox[:4])

            return "{x0:.7f},{y0:.7f},{x1:.7f},{y1:.7f}".format(
                x0=bbox.xmin,
                y0=bbox.ymin,
                x1=bbox.xmax,
                y1=bbox.ymax)
        bbox = BBOXHelper.from_xy([-180, 180, -90, 90])
        return [bbox.xmin, bbox.xmax, bbox.ymin, bbox.ymax, "EPSG:4326"]

    @property
    def bbox_string(self):
        """BBOX is in the format: [x0, y0, x1, y1]. Provides backwards compatibility
        after transition to polygons."""
        if self.bbox_polygon:
            bbox = BBOXHelper.from_xy(self.bbox[:4])

            return "{x0:.7f},{y0:.7f},{x1:.7f},{y1:.7f}".format(
                x0=bbox.xmin,
                y0=bbox.ymin,
                x1=bbox.xmax,
                y1=bbox.ymax)
        bbox = BBOXHelper.from_xy([-180, 180, -90, 90])
        return [bbox.xmin, bbox.xmax, bbox.ymin, bbox.ymax, "EPSG:4326"]

    @property
    def bbox_helper(self):
        if self.bbox_polygon:
            return BBOXHelper(self.bbox_polygon.extent)
        bbox = BBOXHelper.from_xy([-180, 180, -90, 90])
        return [bbox.xmin, bbox.xmax, bbox.ymin, bbox.ymax, "EPSG:4326"]

    @cached_property
    def bbox_x0(self):
        if self.bbox_polygon:
            return self.bbox[0]
        return None

    @cached_property
    def bbox_x1(self):
        if self.bbox_polygon:
            return self.bbox[1]
        return None

    @cached_property
    def bbox_y0(self):
        if self.bbox_polygon:
            return self.bbox[2]
        return None

    @cached_property
    def bbox_y1(self):
        if self.bbox_polygon:
            return self.bbox[3]
        return None

    @property
    def geographic_bounding_box(self):
        """
        Returns an EWKT representation of the bounding box in EPSG:4326
        """
        if self.bbox_polygon:
            bbox = self.bbox_polygon
            if bbox.srid != 4326:
                bbox = bbox.transform(4326, clone=True)
            return str(bbox)
        else:
            bbox = BBOXHelper.from_xy([-180, 180, -90, 90])
            return bbox_to_wkt(
                bbox.xmin,
                bbox.xmax,
                bbox.ymin,
                bbox.ymax,
                srid='EPSG:4326')

    @property
    def license_light(self):
        a = []
        if not self.license:
            return ''
        if (not (self.license.name is None)) and (len(self.license.name) > 0):
            a.append(self.license.name)
        if (not (self.license.url is None)) and (len(self.license.url) > 0):
            a.append("(" + self.license.url + ")")
        return " ".join(a)

    @property
    def license_verbose(self):
        a = []
        if (not (self.license.name_long is None)) and (
                len(self.license.name_long) > 0):
            a.append(self.license.name_long + ":")
        if (not (self.license.description is None)) and (
                len(self.license.description) > 0):
            a.append(self.license.description)
        if (not (self.license.url is None)) and (len(self.license.url) > 0):
            a.append("(" + self.license.url + ")")
        return " ".join(a)

    @property
    def metadata_completeness(self):
        required_fields = [
            'abstract',
            'category',
            'data_quality_statement',
            'date',
            'date_type',
            'language',
            'license',
            'regions',
            'title']
        if self.restriction_code_type == 'otherRestrictions':
            required_fields.append('constraints_other')
        filled_fields = []
        for required_field in required_fields:
            field = getattr(self, required_field, None)
            if field:
                if required_field == 'license':
                    if field.name == 'Not Specified':
                        continue
                if required_field == 'regions':
                    if not field.all():
                        continue
                if required_field == 'category':
                    if not field.identifier:
                        continue
                filled_fields.append(field)
        return '{}%'.format(len(filled_fields) * 100 / len(required_fields))

    @property
    def instance_is_processed(self):
        try:
            if hasattr(self.get_real_instance(), "processed"):
                return self.get_real_instance().processed
            return False
        except Exception:
            return False

    def keyword_list(self):
        return [kw.name for kw in self.keywords.all()]

    def keyword_slug_list(self):
        return [kw.slug for kw in self.keywords.all()]

    def region_name_list(self):
        return [region.name for region in self.regions.all()]

    def spatial_representation_type_string(self):
        if hasattr(self.spatial_representation_type, 'identifier'):
            return self.spatial_representation_type.identifier
        else:
            if hasattr(self, 'storeType'):
                if self.storeType == 'coverageStore':
                    return 'grid'
                return 'vector'
            else:
                return None

    def set_dirty_state(self):
        if not self.dirty_state:
            self.dirty_state = True
            self.save()

    def clear_dirty_state(self):
        if self.dirty_state:
            self.dirty_state = False
            self.save()

    @property
    def keyword_csv(self):
        try:
            keywords_qs = self.get_real_instance().keywords.all()
            if keywords_qs:
                return ','.join([kw.name for kw in keywords_qs])
            else:
                return ''
        except Exception:
            return ''

    def set_bbox_polygon(self, bbox, srid):
        """
        Set `bbox_polygon` from bbox values.

        :param bbox: list or tuple formatted as
            [xmin, ymin, xmax, ymax]
        :param srid: srid as string (e.g. 'EPSG:4326' or '4326')
        """

        bbox_polygon = Polygon.from_bbox(bbox)

        try:
            match = re.match(r'^(EPSG:)?(?P<srid>\d{4,5})$', str(srid))
            bbox_polygon.srid = int(match.group('srid'))
        except AttributeError:
            logger.warning("No srid found for layer %s bounding box", self)

        self.bbox_polygon = bbox_polygon

    def set_bounds_from_center_and_zoom(self, center_x, center_y, zoom):
        """
        Calculate zoom level and center coordinates in mercator.
        """
        self.center_x = center_x
        self.center_y = center_y
        self.zoom = zoom

        deg_len_equator = 40075160.0 / 360.0

        # covert center in lat lon
        def get_lon_lat():
            wgs84 = Proj(init='epsg:4326')
            mercator = Proj(init='epsg:3857')
            lon, lat = transform(mercator, wgs84, center_x, center_y)
            return lon, lat

        # calculate the degree length at this latitude
        def deg_len():
            lon, lat = get_lon_lat()
            return math.cos(lat) * deg_len_equator

        lon, lat = get_lon_lat()

        # taken from http://wiki.openstreetmap.org/wiki/Zoom_levels
        # it might be not precise but enough for the purpose
        distance_per_pixel = 40075160 * math.cos(lat) / 2 ** (zoom + 8)

        # calculate the distance from the center of the map in degrees
        # we use the calculated degree length on the x axis and the
        # normal degree length on the y axis assumin that it does not change

        # Assuming a map of 1000 px of width and 700 px of height
        distance_x_degrees = distance_per_pixel * 500.0 / deg_len()
        distance_y_degrees = distance_per_pixel * 350.0 / deg_len_equator

        bbox_x0 = lon - distance_x_degrees
        bbox_x1 = lon + distance_x_degrees
        bbox_y0 = lat - distance_y_degrees
        bbox_y1 = lat + distance_y_degrees
        self.srid = 'EPSG:4326'
        self.set_bbox_polygon((bbox_x0, bbox_y0, bbox_x1, bbox_y1), self.srid)

    def set_bounds_from_bbox(self, bbox, srid):
        """
        Calculate zoom level and center coordinates in mercator.

        :param bbox: BBOX is either a `geos.Pologyon` or in the
            format: [x0, x1, y0, y1], which is:
            [min lon, max lon, min lat, max lat] or
            [xmin, xmax, ymin, ymax]
        :type bbox: list
        """
        if isinstance(bbox, Polygon):
            self.set_bbox_polygon(bbox.extent, srid)
            self.set_center_zoom()
            return
        elif isinstance(bbox, list):
            self.set_bbox_polygon([bbox[0], bbox[2], bbox[1], bbox[3]], srid)
            self.set_center_zoom()
            return

        if not bbox or len(bbox) < 4:
            raise ValidationError(
                'Bounding Box cannot be empty %s for a given resource' %
                self.name)
        if not srid:
            raise ValidationError(
                'Projection cannot be empty %s for a given resource' %
                self.name)

        self.srid = srid
        self.set_bbox_polygon(
            (bbox[0], bbox[2], bbox[1], bbox[3]), srid)
        self.set_center_zoom()

    def set_center_zoom(self):
        """
        Sets the center coordinates and zoom level in EPSG4326
        """
        bbox = self.bbox_polygon
        center_x, center_y = self.bbox_polygon.centroid.coords
        try:
            center = Point(center_x, center_y, srid=self.bbox_polygon.srid)
            center.transform(self.srid)
        except Exception:
            center = Point(center_x, center_y, srid=self.srid)

        if bbox.srid != 4326:
            bbox = bbox.transform(4326, clone=True)

        self.center_x, self.center_y = center.coords

        try:
            ext = bbox.extent
            width_zoom = math.log(360 / (ext[2] - ext[0]), 2)
            height_zoom = math.log(360 / (ext[3] - ext[1]), 2)
            self.zoom = math.ceil(min(width_zoom, height_zoom))
        except ZeroDivisionError:
            pass

    def download_links(self):
        """assemble download links for pycsw"""
        links = []
        for link in self.link_set.all():
            if link.link_type == 'metadata':  # avoid recursion
                continue
            if link.link_type == 'html':
                links.append(
                    (self.title,
                     'Web address (URL)',
                     'WWW:LINK-1.0-http--link',
                     link.url))
            elif link.link_type in ('OGC:WMS', 'OGC:WFS', 'OGC:WCS'):
                links.append((self.title, link.name, link.link_type, link.url))
            else:
                _link_type = 'WWW:DOWNLOAD-1.0-http--download'
                if self.storeType == 'remoteStore' and link.extension in ('html'):
                    _link_type = 'WWW:DOWNLOAD-%s' % self.remote_service.type
                description = '%s (%s Format)' % (self.title, link.name)
                links.append(
                    (self.title,
                     description,
                     _link_type,
                     link.url))
        return links

    def get_tiles_url(self):
        """Return URL for Z/Y/X mapping clients or None if it does not exist.
        """
        try:
            tiles_link = self.link_set.get(name='Tiles')
        except Link.DoesNotExist:
            return None
        else:
            return tiles_link.url

    def get_legend(self):
        """Return Link for legend or None if it does not exist.
        """
        try:
            legends_link = self.link_set.filter(name='Legend')
        except Link.DoesNotExist:
            tb = traceback.format_exc()
            logger.debug(tb)
            return None
        except Link.MultipleObjectsReturned:
            tb = traceback.format_exc()
            logger.debug(tb)
            return None
        else:
            return legends_link

    def get_legend_url(self, style_name=None):
        """Return URL for legend or None if it does not exist.

           The legend can be either an image (for Geoserver's WMS)
           or a JSON object for ArcGIS.
        """
        legend = self.get_legend()

        if legend is None:
            return None

        if legend.count() > 0:
            if not style_name:
                return legend.first().url
            else:
                for _legend in legend:
                    if style_name in _legend.url:
                        return _legend.url
        return None

    def get_ows_url(self):
        """Return URL for OGC WMS server None if it does not exist.
        """
        try:
            ows_link = self.link_set.get(name='OGC:WMS')
        except Link.DoesNotExist:
            return None
        else:
            return ows_link.url

    def get_thumbnail_url(self):
        """Return a thumbnail url.

           It could be a local one if it exists, a remote one (WMS GetImage) for example
           or a 'Missing Thumbnail' one.
        """
        _thumbnail_url = self.thumbnail_url or staticfiles.static(settings.MISSING_THUMBNAIL)
        local_thumbnails = self.link_set.filter(name='Thumbnail')
        remote_thumbnails = self.link_set.filter(name='Remote Thumbnail')
        if local_thumbnails.count() > 0:
            _thumbnail_url = add_url_params(
                local_thumbnails[0].url, {'v': str(uuid.uuid4())[:8]})
        elif remote_thumbnails.count() > 0:
            _thumbnail_url = add_url_params(
                remote_thumbnails[0].url, {'v': str(uuid.uuid4())[:8]})
        return _thumbnail_url

    def has_thumbnail(self):
        """Determine if the thumbnail object exists and an image exists"""
        return self.link_set.filter(name='Thumbnail').exists()

    # Note - you should probably broadcast layer#post_save() events to ensure
    # that indexing (or other listeners) are notified
    def save_thumbnail(self, filename, image):
        upload_path = thumb_path(filename)

        try:
            # Check that the image is valid
            content_data = BytesIO(image)
            im = Image.open(content_data)
            im.verify()  # verify that it is, in fact an image

            name, ext = os.path.splitext(filename)
            remove_thumbs(name)

            if upload_path and image:
                actual_name = storage.save(upload_path, ContentFile(image))
                url = storage.url(actual_name)
                _url = urlparse(url)
                _upload_path = thumb_path(os.path.basename(_url.path))
                if upload_path != _upload_path:
                    if storage.exists(_upload_path):
                        storage.delete(_upload_path)
                    try:
                        os.rename(
                            storage.path(upload_path),
                            storage.path(_upload_path)
                        )
                    except Exception as e:
                        logger.debug(e)

                try:
                    # Optimize the Thumbnail size and resolution
                    _default_thumb_size = getattr(
                        settings, 'THUMBNAIL_GENERATOR_DEFAULT_SIZE', {'width': 240, 'height': 200})
                    im = Image.open(open(storage.path(_upload_path), mode='rb'))
                    im.thumbnail(
                        (_default_thumb_size['width'], _default_thumb_size['height']),
                        resample=Image.ANTIALIAS)
                    cover = resizeimage.resize_cover(
                        im,
                        [_default_thumb_size['width'], _default_thumb_size['height']])
                    cover.save(storage.path(_upload_path), format='JPEG')
                except Exception as e:
                    logger.debug(e)

                # check whether it is an URI or not
                parsed = urlsplit(url)
                if not parsed.netloc:
                    # assuming is a relative path to current site
                    site_url = settings.SITEURL.rstrip('/') if settings.SITEURL.startswith('http') else settings.SITEURL
                    url = urljoin(site_url, url)

                # should only have one 'Thumbnail' link
                _links = Link.objects.filter(resource=self, name='Thumbnail')
                if _links and _links.count() > 1:
                    _links.delete()
                obj, created = Link.objects.get_or_create(
                    resource=self,
                    name='Thumbnail',
                    defaults=dict(
                        url=url,
                        extension='png',
                        mime='image/png',
                        link_type='image',
                    )
                )
                self.thumbnail_url = url
                obj.url = url
                obj.save()
                ResourceBase.objects.filter(id=self.id).update(
                    thumbnail_url=url
                )
        except Exception as e:
            logger.debug(
                'Error when generating the thumbnail for resource %s. (%s)' %
                (self.id, str(e)))
            logger.warn('Check permissions for file %s.' % upload_path)
            try:
                Link.objects.filter(resource=self, name='Thumbnail').delete()
                _thumbnail_url = staticfiles.static(settings.MISSING_THUMBNAIL)
                obj, created = Link.objects.get_or_create(
                    resource=self,
                    name='Thumbnail',
                    defaults=dict(
                        url=_thumbnail_url,
                        extension='png',
                        mime='image/png',
                        link_type='image',
                    )
                )
                self.thumbnail_url = _thumbnail_url
                obj.url = _thumbnail_url
                obj.save()
                ResourceBase.objects.filter(id=self.id).update(
                    thumbnail_url=_thumbnail_url
                )
            except Exception as e:
                logger.debug(
                    'Error when generating the thumbnail for resource %s. (%s)' %
                    (self.id, str(e)))

    def set_missing_info(self):
        """Set default permissions and point of contacts.

           It is mandatory to call it from descendant classes
           but hard to enforce technically via signals or save overriding.
        """
        from guardian.models import UserObjectPermission
        logger.debug('Checking for permissions.')
        #  True if every key in the get_all_level_info dict is empty.
        no_custom_permissions = UserObjectPermission.objects.filter(
            content_type=ContentType.objects.get_for_model(
                self.get_self_resource()), object_pk=str(
                self.pk)).exists()

        if not no_custom_permissions:
            logger.debug(
                'There are no permissions for this object, setting default perms.')
            self.set_default_permissions()

        user = None
        if self.owner:
            user = self.owner
        else:
            try:
                user = ResourceBase.objects.admin_contact().user
            except Exception:
                pass

        if user:
            if self.poc is None:
                self.poc = user
            if self.metadata_author is None:
                self.metadata_author = user

    def maintenance_frequency_title(self):
        return [v for v in UPDATE_FREQUENCIES if v[0] == self.maintenance_frequency][0][1].title()

    def language_title(self):
        return [v for v in ALL_LANGUAGES if v[0] == self.language][0][1].title()

    def _set_poc(self, poc):
        # reset any poc assignation to this resource
        ContactRole.objects.filter(
            role='pointOfContact',
            resource=self).delete()
        # create the new assignation
        ContactRole.objects.create(
            role='pointOfContact',
            resource=self,
            contact=poc)

    def _get_poc(self):
        try:
            the_poc = ContactRole.objects.get(
                role='pointOfContact', resource=self).contact
        except ContactRole.DoesNotExist:
            the_poc = None
        return the_poc

    poc = property(_get_poc, _set_poc)

    def _set_metadata_author(self, metadata_author):
        # reset any metadata_author assignation to this resource
        ContactRole.objects.filter(role='author', resource=self).delete()
        # create the new assignation
        ContactRole.objects.create(
            role='author',
            resource=self,
            contact=metadata_author)

    def _get_metadata_author(self):
        try:
            the_ma = ContactRole.objects.get(
                role='author', resource=self).contact
        except ContactRole.DoesNotExist:
            the_ma = None
        return the_ma

    def handle_moderated_uploads(self):
        if settings.RESOURCE_PUBLISHING:
            self.is_published = False
        if settings.ADMIN_MODERATE_UPLOADS:
            self.is_approved = False

    def add_missing_metadata_author_or_poc(self):
        """
        Set metadata_author and/or point of contact (poc) to a resource when any of them is missing
        """
        if not self.metadata_author:
            self.metadata_author = self.owner
        if not self.poc:
            self.poc = self.owner

    metadata_author = property(_get_metadata_author, _set_metadata_author)


class LinkManager(models.Manager):
    """Helper class to access links grouped by type
    """

    def data(self):
        return self.get_queryset().filter(link_type='data')

    def image(self):
        return self.get_queryset().filter(link_type='image')

    def download(self):
        return self.get_queryset().filter(link_type__in=['image', 'data', 'original'])

    def metadata(self):
        return self.get_queryset().filter(link_type='metadata')

    def original(self):
        return self.get_queryset().filter(link_type='original')

    def ows(self):
        return self.get_queryset().filter(
            link_type__in=['OGC:WMS', 'OGC:WFS', 'OGC:WCS'])


class Link(models.Model):
    """Auxiliary model for storing links for resources.

       This helps avoiding the need for runtime lookups
       to the OWS server or the CSW Catalogue.

       There are four types of links:
        * original: For uploaded files (Shapefiles or GeoTIFFs)
        * data: For WFS and WCS links that allow access to raw data
        * image: For WMS and TMS links
        * metadata: For CSW links
        * OGC:WMS: for WMS service links
        * OGC:WFS: for WFS service links
        * OGC:WCS: for WCS service links
    """
    resource = models.ForeignKey(
        ResourceBase,
        blank=True,
        null=True,
        on_delete=models.CASCADE)
    extension = models.CharField(
        max_length=255,
        help_text=_('For example "kml"'))
    link_type = models.CharField(
        max_length=255, choices=[
            (x, x) for x in LINK_TYPES])
    name = models.CharField(max_length=255, help_text=_(
        'For example "View in Google Earth"'))
    mime = models.CharField(max_length=255,
                            help_text=_('For example "text/xml"'))
    url = models.TextField(max_length=1000)

    objects = LinkManager()

    def __str__(self):
        return "{0} link".format(self.link_type)


class MenuPlaceholder(models.Model):
    name = models.CharField(
        max_length=255,
        null=False,
        blank=False,
        unique=True
    )

    def __str__(self):
        return "{0}".format(self.name)


class Menu(models.Model):
    title = models.CharField(
        max_length=255,
        null=False,
        blank=False
    )
    placeholder = models.ForeignKey(
        to='MenuPlaceholder',
        on_delete=models.CASCADE,
        null=False
    )
    order = models.IntegerField(
        null=False,
    )

    def __str__(self):
        return "{0}".format(self.title)

    class Meta:
        unique_together = (
            ('placeholder', 'order'),
            ('placeholder', 'title'),
        )
        ordering = ['order']


class MenuItem(models.Model):
    title = models.CharField(
        max_length=255,
        null=False,
        blank=False
    )
    menu = models.ForeignKey(
        to='Menu',
        null=False,
        on_delete=models.CASCADE
    )
    order = models.IntegerField(
        null=False
    )
    blank_target = models.BooleanField()
    url = models.CharField(
        max_length=2000,
        null=False,
        blank=False
    )

    def __eq__(self, other):
        return self.order == other.order

    def __ne__(self, other):
        return self.order != other.order

    def __lt__(self, other):
        return self.order < other.order

    def __le__(self, other):
        return self.order <= other.order

    def __gt__(self, other):
        return self.order > other.order

    def __ge__(self, other):
        return self.order >= other.order

    def __hash__(self):
        return hash(self.url)

    def __str__(self):
        return "{0}".format(self.title)

    class Meta:
        unique_together = (
            ('menu', 'order'),
            ('menu', 'title'),
        )
        ordering = ['order']


class CuratedThumbnail(models.Model):
    resource = models.OneToOneField(ResourceBase, on_delete=models.CASCADE)
    img = models.ImageField(upload_to='curated_thumbs')
    # TOD read thumb size from settings
    img_thumbnail = ImageSpecField(source='img',
                                   processors=[ResizeToFill(240, 180)],
                                   format='PNG',
                                   options={'quality': 60})

    @property
    def thumbnail_url(self):
        try:
            if not Simple()._exists(self.img_thumbnail):
                Simple().generate(self.img_thumbnail, force=True)
            upload_path = storage.path(self.img_thumbnail.name)
            actual_name = os.path.basename(storage.url(upload_path))
            _upload_path = os.path.join(os.path.dirname(upload_path), actual_name)
            if not os.path.exists(_upload_path):
                os.rename(upload_path, _upload_path)
            return self.img_thumbnail.url
        except Exception as e:
            logger.exception(e)
        return ''


class Configuration(SingletonModel):
    """
    A model used for managing the Geonode instance's global configuration,
    without a need for reloading the instance.

    Usage:
    from geonode.base.models import Configuration
    config = Configuration.load()
    """
    read_only = models.BooleanField(default=False)
    maintenance = models.BooleanField(default=False)

    class Meta:
        verbose_name_plural = 'Configuration'

    def __str__(self):
        return 'Configuration'


class UserGeoLimit(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=False,
        blank=False,
        on_delete=models.CASCADE)
    resource = models.ForeignKey(
        ResourceBase,
        null=False,
        blank=False,
        on_delete=models.CASCADE)
    wkt = models.TextField(
        db_column='wkt',
        blank=True)


class GroupGeoLimit(models.Model):
    group = models.ForeignKey(
        GroupProfile,
        null=False,
        blank=False,
        on_delete=models.CASCADE)
    resource = models.ForeignKey(
        ResourceBase,
        null=False,
        blank=False,
        on_delete=models.CASCADE)
    wkt = models.TextField(
        db_column='wkt',
        blank=True)


def resourcebase_post_save(instance, *args, **kwargs):
    """
    Used to fill any additional fields after the save.
    Has to be called by the children
    """
    try:
        # set default License if no specified
        if instance.license is None:
            license = License.objects.filter(name="Not Specified")

            if license and len(license) > 0:
                instance.license = license[0]

        ResourceBase.objects.filter(id=instance.id).update(
            thumbnail_url=instance.get_thumbnail_url(),
            detail_url=instance.get_absolute_url(),
            csw_insert_date=now(),
            license=instance.license)
        instance.refresh_from_db()
    except Exception:
        tb = traceback.format_exc()
        if tb:
            logger.debug(tb)
    finally:
        instance.set_missing_info()

    try:
        if not instance.regions or instance.regions.count() == 0:
            srid1, wkt1 = instance.geographic_bounding_box.split(";")
            srid1 = re.findall(r'\d+', srid1)

            poly1 = GEOSGeometry(wkt1, srid=int(srid1[0]))
            poly1.transform(4326)

            queryset = Region.objects.all().order_by('name')
            global_regions = []
            regions_to_add = []
            for region in queryset:
                try:
                    srid2, wkt2 = region.geographic_bounding_box.split(";")
                    srid2 = re.findall(r'\d+', srid2)

                    poly2 = GEOSGeometry(wkt2, srid=int(srid2[0]))
                    poly2.transform(4326)

                    if poly2.intersection(poly1):
                        regions_to_add.append(region)
                    if region.level == 0 and region.parent is None:
                        global_regions.append(region)
                except Exception:
                    tb = traceback.format_exc()
                    if tb:
                        logger.debug(tb)
            if regions_to_add or global_regions:
                if regions_to_add and len(
                        regions_to_add) > 0 and len(regions_to_add) <= 30:
                    instance.regions.add(*regions_to_add)
                else:
                    instance.regions.add(*global_regions)
    except Exception:
        tb = traceback.format_exc()
        if tb:
            logger.debug(tb)
    finally:
        # refresh catalogue metadata records
        from geonode.catalogue.models import catalogue_post_save
        catalogue_post_save(instance=instance, sender=instance.__class__)


def rating_post_save(instance, *args, **kwargs):
    """
    Used to fill the average rating field on OverallRating change.
    """
    ResourceBase.objects.filter(
        id=instance.object_id).update(
        rating=instance.rating)


signals.post_save.connect(rating_post_save, sender=OverallRating)
