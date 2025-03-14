# -*- coding: utf-8 -*-
import hashlib
import itertools
import os
import re
import time
import uuid

from datetime import datetime
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import models, transaction
from django.db.models import F, Max, Q, signals as dbsignals
from django.dispatch import receiver
from django.utils import translation
from django.utils.functional import cached_property
from django.utils.translation import trans_real, ugettext_lazy as _

from django_jsonfield_backport.models import JSONField
from django_statsd.clients import statsd

import olympia.core.logger

from olympia import activity, amo, core
from olympia.access import acl
from olympia.addons.utils import generate_addon_guid
from olympia.amo.decorators import use_primary_db
from olympia.amo.fields import PositiveAutoField
from olympia.amo.models import (
    BasePreview,
    BaseQuerySet,
    FilterableManyToManyField,
    LongNameIndex,
    ManagerBase,
    ModelBase,
    OnChangeMixin,
    SaveUpdateMixin,
)
from olympia.amo.templatetags import jinja_helpers
from olympia.amo.urlresolvers import reverse
from olympia.amo.utils import (
    StopWatch,
    attach_trans_dict,
    find_language,
    send_mail,
    slugify,
    sorted_groupby,
    timer,
    to_language,
)
from olympia.constants.categories import CATEGORIES_BY_ID
from olympia.constants.promoted import NOT_PROMOTED, RECOMMENDED
from olympia.constants.reviewers import REPUTATION_CHOICES
from olympia.files.models import File
from olympia.files.utils import extract_translations, resolve_i18n_message
from olympia.ratings.models import Rating
from olympia.tags.models import Tag
from olympia.translations.fields import (
    LinkifiedField,
    PurifiedField,
    TranslatedField,
    save_signal,
)
from olympia.translations.hold import translation_saved
from olympia.translations.models import Translation
from olympia.users.models import UserProfile
from olympia.versions.compare import version_int
from olympia.versions.models import (
    Version,
    VersionPreview,
    VersionReviewerFlags,
    inherit_nomination,
)

from . import signals


log = olympia.core.logger.getLogger('z.addons')


MAX_SLUG_INCREMENT = 999
SLUG_INCREMENT_SUFFIXES = set(range(1, MAX_SLUG_INCREMENT + 1))
GUID_REUSE_FORMAT = 'guid-reused-by-pk-{}'


def get_random_slug():
    """Return a 20 character long random string"""
    return ''.join(str(uuid.uuid4()).split('-')[:-1])


def clean_slug(instance, slug_field='slug'):
    """Cleans a model instance slug.

    This strives to be as generic as possible but is only used
    by Add-ons at the moment.

    :param instance: The instance to clean the slug for.
    :param slug_field: The field where to get the currently set slug from.
    """
    slug = getattr(instance, slug_field, None) or instance.name

    if not slug:
        # Initialize the slug with what we have available: a name translation
        # or in last resort a random slug.
        translations = Translation.objects.filter(id=instance.name_id)
        if translations.exists():
            slug = translations[0]

    max_length = instance._meta.get_field(slug_field).max_length
    # We have to account for slug being reduced to '' by slugify
    slug = slugify(slug or '')[:max_length] or get_random_slug()

    if DeniedSlug.blocked(slug):
        slug = slug[: max_length - 1] + '~'

    # The following trick makes sure we are using a manager that returns
    # all the objects, as otherwise we could have a slug clash on our hands.
    # Eg with the "Addon.objects" manager, which doesn't list deleted addons,
    # we could have a "clean" slug which is in fact already assigned to an
    # already existing (deleted) addon. Also, make sure we use the base class.
    manager = models.Manager()
    manager.model = instance._meta.proxy_for_model or instance.__class__

    qs = manager.values_list(slug_field, flat=True)  # Get list of all slugs.
    if instance.id:
        qs = qs.exclude(pk=instance.id)  # Can't clash with itself.

    # We first need to make sure there's a clash, before trying to find a
    # suffix that is available. Eg, if there's a "foo-bar" slug, "foo" is still
    # available.
    clash = qs.filter(**{slug_field: slug})

    if clash.exists():
        max_postfix_length = len(str(MAX_SLUG_INCREMENT))

        slug = slugify(slug)[: max_length - max_postfix_length]

        # There is a clash, so find a suffix that will make this slug unique.
        lookup = {'%s__startswith' % slug_field: slug}
        clashes = qs.filter(**lookup)

        prefix_len = len(slug)
        used_slug_numbers = [value[prefix_len:] for value in clashes]

        # find the next free slug number
        slug_numbers = {int(i) for i in used_slug_numbers if i.isdigit()}
        unused_numbers = SLUG_INCREMENT_SUFFIXES - slug_numbers

        if unused_numbers:
            num = min(unused_numbers)
        elif max_length is None:
            num = max(slug_numbers) + 1
        else:
            # This could happen. The current implementation (using
            # ``[:max_length -2]``) only works for the first 100 clashes in the
            # worst case (if the slug is equal to or longuer than
            # ``max_length - 2`` chars).
            # After that, {verylongslug}-100 will be trimmed down to
            # {verylongslug}-10, which is already assigned, but it's the last
            # solution tested.
            raise RuntimeError('No suitable slug increment for {} found'.format(slug))

        slug = '{slug}{postfix}'.format(slug=slug, postfix=num)

    setattr(instance, slug_field, slug)

    return instance


class AddonQuerySet(BaseQuerySet):
    def id_or_slug(self, val):
        """Get add-ons by id or slug."""
        if isinstance(val, str) and not val.isdigit():
            return self.filter(slug=val)
        return self.filter(id=val)

    def public(self):
        """Get reviewed add-ons only"""
        return self.filter(self.valid_q(amo.REVIEWED_STATUSES))

    def valid(self):
        """Get valid, enabled add-ons only"""
        return self.filter(self.valid_q(amo.VALID_ADDON_STATUSES))

    def not_disabled_by_mozilla(self):
        """Get all add-ons not disabled by Mozilla."""
        return self.exclude(status=amo.STATUS_DISABLED)

    def valid_q(self, statuses):
        """
        Return a Q object that selects a valid Addon with the given statuses.

        An add-on is valid if not disabled and has a current version.
        """
        return Q(
            _current_version__isnull=False, disabled_by_user=False, status__in=statuses
        )


class AddonManager(ManagerBase):
    _queryset_class = AddonQuerySet

    def __init__(self, include_deleted=False):
        # DO NOT change the default value of include_deleted unless you've read
        # through the comment just above the Addon managers
        # declaration/instantiation and understand the consequences.
        ManagerBase.__init__(self)
        self.include_deleted = include_deleted

    def get_queryset(self):
        qs = super(AddonManager, self).get_queryset()
        if not self.include_deleted:
            qs = qs.exclude(status=amo.STATUS_DELETED)
        return qs.transform(Addon.transformer)

    def id_or_slug(self, val):
        """Get add-ons by id or slug."""
        return self.get_queryset().id_or_slug(val)

    def public(self):
        """Get public add-ons only"""
        return self.get_queryset().public()

    def valid(self):
        """Get valid, enabled add-ons only"""
        return self.get_queryset().valid()

    def not_disabled_by_mozilla(self):
        """Get all add-ons not disabled by Mozilla."""
        return self.get_queryset().not_disabled_by_mozilla()

    def get_base_queryset_for_queue(
        self,
        admin_reviewer=False,
        admin_content_review=False,
        show_pending_rejection=False,
    ):
        qs = (
            self.get_queryset()
            # We don't want the default transformer, it does too much, and
            # crucially, it prevents the
            # select_related('_current_version__autoapprovalsummary') from
            # working, because it overrides the _current_version with the one
            # it fetches. We want translations though.
            .only_translations()
            # We need those joins for the queue to work without making extra
            # queries.
            .select_related(
                'addonapprovalscounter',
                'reviewerflags',
                '_current_version',
                '_current_version__autoapprovalsummary',
                '_current_version__reviewerflags',
                'promotedaddon',
            ).prefetch_related(
                '_current_version__files',
            )
        )
        if not show_pending_rejection:
            qs = qs.filter(
                _current_version__reviewerflags__pending_rejection__isnull=True
            )
        if not admin_reviewer:
            if admin_content_review:
                qs = qs.exclude(reviewerflags__needs_admin_content_review=True)
            else:
                qs = qs.exclude(reviewerflags__needs_admin_code_review=True)
        return qs

    def get_auto_approved_queue(self, admin_reviewer=False):
        """Return a queryset of Addon objects that have been auto-approved but
        not confirmed by a human yet."""
        success_verdict = amo.AUTO_APPROVED
        qs = (
            self.get_base_queryset_for_queue(admin_reviewer=admin_reviewer)
            .public()
            .filter(_current_version__autoapprovalsummary__verdict=success_verdict)
            .exclude(_current_version__autoapprovalsummary__confirmed=True)
            .order_by(
                '-_current_version__autoapprovalsummary__score',
                '-_current_version__autoapprovalsummary__weight',
                'addonapprovalscounter__last_human_review',
                'created',
            )
        )
        return qs

    def get_content_review_queue(self, admin_reviewer=False):
        """Return a queryset of Addon objects that need content review."""
        qs = (
            self.get_base_queryset_for_queue(
                admin_reviewer=admin_reviewer, admin_content_review=True
            )
            .valid()
            .filter(
                addonapprovalscounter__last_content_review=None,
                # Only content review extensions and dictionaries. See
                # https://github.com/mozilla/addons-server/issues/11796 &
                # https://github.com/mozilla/addons-server/issues/12065
                type__in=(amo.ADDON_EXTENSION, amo.ADDON_DICT),
            )
            .order_by('created')
        )
        return qs

    def get_scanners_queue(self, admin_reviewer=False):
        """Return a queryset of Addon objects that have been approved but
        contain versions that were automatically flagged as needing human
        review (regardless of channel)."""
        return (
            self.get_base_queryset_for_queue(admin_reviewer=admin_reviewer)
            # All valid statuses, plus incomplete as well because the add-on
            # could be purely unlisted (so we can't use valid_q(), which
            # filters out current_version=None). We know the add-ons are likely
            # to have a version since they got the needs_human_review flag, so
            # returning incomplete ones is acceptable.
            .filter(
                status__in=[amo.STATUS_APPROVED, amo.STATUS_NOMINATED, amo.STATUS_NULL],
                versions__files__status__in=[
                    amo.STATUS_APPROVED,
                    amo.STATUS_AWAITING_REVIEW,
                ],
                versions__needs_human_review=True,
            ).order_by('created')
            # There could be several versions matching for a single add-on so
            # we need a distinct.
            .distinct()
        )

    def get_mad_queue(self, admin_reviewer=False):
        return (
            self.get_base_queryset_for_queue(admin_reviewer=admin_reviewer)
            # All valid statuses, plus incomplete as well because the add-on
            # could be purely unlisted (so we can't use valid_q(), which
            # filters out current_version=None). We know the add-ons are likely
            # to have a version since they got the needs_human_review_by_mad
            # flag, so returning incomplete ones is acceptable.
            .filter(
                Q(
                    status__in=[
                        amo.STATUS_APPROVED,
                        amo.STATUS_NOMINATED,
                        amo.STATUS_NULL,
                    ]
                ),
                Q(
                    versions__files__status__in=[
                        amo.STATUS_APPROVED,
                        amo.STATUS_AWAITING_REVIEW,
                    ]
                ),
                (
                    Q(
                        versions__reviewerflags__needs_human_review_by_mad=True,
                        versions__channel=amo.RELEASE_CHANNEL_UNLISTED,
                    )
                    | Q(
                        _current_version__reviewerflags__needs_human_review_by_mad=(  # noqa
                            True
                        )
                    )
                ),
            ).order_by('created')
            # There could be several versions matching for a single add-on so
            # we need a distinct.
            .distinct()
        )

    def get_pending_rejection_queue(self, admin_reviewer=False):
        filter_kwargs = {
            '_current_version__reviewerflags__pending_rejection__isnull': False
        }
        return (
            self.get_base_queryset_for_queue(
                admin_reviewer=admin_reviewer,
                show_pending_rejection=True,
            )
            .filter(**filter_kwargs)
            .order_by('_current_version__reviewerflags__pending_rejection')
        )


class Addon(OnChangeMixin, ModelBase):
    id = PositiveAutoField(primary_key=True)
    STATUS_CHOICES = amo.STATUS_CHOICES_ADDON

    guid = models.CharField(max_length=255, unique=True, null=True)
    slug = models.CharField(max_length=30, unique=True, null=True)
    name = TranslatedField()
    default_locale = models.CharField(
        max_length=10, default=settings.LANGUAGE_CODE, db_column='defaultlocale'
    )

    type = models.PositiveIntegerField(
        choices=amo.ADDON_TYPE.items(),
        db_column='addontype_id',
        default=amo.ADDON_EXTENSION,
    )
    status = models.PositiveIntegerField(
        choices=STATUS_CHOICES.items(), default=amo.STATUS_NULL
    )
    icon_type = models.CharField(max_length=25, blank=True, db_column='icontype')
    icon_hash = models.CharField(max_length=8, blank=True, null=True)
    homepage = TranslatedField()
    support_email = TranslatedField(db_column='supportemail')
    support_url = TranslatedField(db_column='supporturl')
    description = PurifiedField(short=False)

    summary = LinkifiedField()
    developer_comments = PurifiedField(db_column='developercomments')
    eula = PurifiedField()
    privacy_policy = PurifiedField(db_column='privacypolicy')

    average_rating = models.FloatField(
        max_length=255, default=0, null=True, db_column='averagerating'
    )
    bayesian_rating = models.FloatField(default=0, db_column='bayesianrating')
    total_ratings = models.PositiveIntegerField(default=0, db_column='totalreviews')
    text_ratings_count = models.PositiveIntegerField(
        default=0, db_column='textreviewscount'
    )
    weekly_downloads = models.PositiveIntegerField(
        default=0, db_column='weeklydownloads'
    )
    hotness = models.FloatField(default=0)

    average_daily_users = models.PositiveIntegerField(default=0)

    last_updated = models.DateTimeField(
        null=True, help_text='Last time this add-on had a file/version update'
    )

    disabled_by_user = models.BooleanField(default=False, db_column='inactive')

    target_locale = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text='For dictionaries and language packs. Identifies the '
        'language and, optionally, region that this add-on is '
        'written for. Examples: en-US, fr, and de-AT',
    )

    contributions = models.URLField(max_length=255, blank=True)

    authors = FilterableManyToManyField(
        'users.UserProfile',
        through='AddonUser',
        related_name='addons',
        q_filter=~Q(addonuser__role=amo.AUTHOR_ROLE_DELETED),
    )

    _current_version = models.ForeignKey(
        Version,
        db_column='current_version',
        related_name='+',
        null=True,
        on_delete=models.SET_NULL,
    )

    is_experimental = models.BooleanField(default=False, db_column='experimental')
    reputation = models.SmallIntegerField(
        default=0,
        null=True,
        choices=REPUTATION_CHOICES.items(),
        help_text='The higher the reputation value, the further down the '
        'add-on will be in the auto-approved review queue. '
        'A value of 0 has no impact',
    )
    requires_payment = models.BooleanField(default=False)

    unfiltered = AddonManager(include_deleted=True)
    objects = AddonManager()

    class Meta:
        db_table = 'addons'
        # This is very important:
        # The default base manager will be used for relations like
        # `version.addon`. We thus want one that is NOT filtered in any case,
        # we don't want a 500 if the addon is not found (because it has the
        # status amo.STATUS_DELETED for example).
        # The CLASS of the one configured here will also be used for "many to
        # many relations" like `collection.addons`. In that case, we do want
        # the filtered version by default, to make sure we're not displaying
        # stuff by mistake. You thus want the filtered one configured
        # as `base_manager_name`.
        # We don't control the instantiation, but AddonManager sets
        # include_deleted to False by default, so filtering is enabled by
        # default.
        base_manager_name = 'unfiltered'
        indexes = [
            models.Index(fields=('bayesian_rating',), name='bayesianrating'),
            models.Index(fields=('created',), name='created_idx'),
            models.Index(fields=('_current_version',), name='current_version'),
            models.Index(fields=('disabled_by_user',), name='inactive'),
            models.Index(fields=('hotness',), name='hotness_idx'),
            models.Index(fields=('last_updated',), name='last_updated'),
            models.Index(fields=('modified',), name='modified_idx'),
            models.Index(fields=('status',), name='status'),
            models.Index(fields=('target_locale',), name='target_locale'),
            models.Index(fields=('type',), name='addontype_id'),
            models.Index(fields=('weekly_downloads',), name='weeklydownloads_idx'),
            models.Index(fields=('average_daily_users', 'type'), name='adus_type_idx'),
            models.Index(fields=('bayesian_rating', 'type'), name='rating_type_idx'),
            models.Index(fields=('created', 'type'), name='created_type_idx'),
            models.Index(fields=('last_updated', 'type'), name='last_updated_type_idx'),
            models.Index(fields=('modified', 'type'), name='modified_type_idx'),
            models.Index(
                fields=('type', 'status', 'disabled_by_user'),
                name='type_status_inactive_idx',
            ),
            models.Index(
                fields=('weekly_downloads', 'type'), name='downloads_type_idx'
            ),
            models.Index(
                fields=('type', 'status', 'disabled_by_user', '_current_version'),
                name='visible_idx',
            ),
            models.Index(fields=('name', 'status', 'type'), name='name_2'),
        ]

    def __str__(self):
        return '%s: %s' % (self.id, self.name)

    def __init__(self, *args, **kw):
        super(Addon, self).__init__(*args, **kw)

    def save(self, **kw):
        self.clean_slug()
        super(Addon, self).save(**kw)

    @use_primary_db
    def clean_slug(self, slug_field='slug'):
        if self.status == amo.STATUS_DELETED:
            return

        clean_slug(self, slug_field)

    def force_disable(self):
        activity.log_create(amo.LOG.CHANGE_STATUS, self, amo.STATUS_DISABLED)
        log.info('Addon "%s" status changed to: %s', self.slug, amo.STATUS_DISABLED)
        self.update(status=amo.STATUS_DISABLED)
        self.update_version()
        # See: https://github.com/mozilla/addons-server/issues/13194
        self.disable_all_files()

    def force_enable(self):
        activity.log_create(amo.LOG.CHANGE_STATUS, self, amo.STATUS_APPROVED)
        log.info('Addon "%s" status changed to: %s', self.slug, amo.STATUS_APPROVED)
        self.update(status=amo.STATUS_APPROVED)
        # Call update_status() to fix the status if the add-on is not actually
        # in a state that allows it to be public.
        self.update_status()

    def deny_resubmission(self):
        if self.is_guid_denied:
            raise RuntimeError('GUID already denied')

        activity.log_create(amo.LOG.DENIED_GUID_ADDED, self)
        log.info('Deny resubmission for addon "%s"', self.slug)
        DeniedGuid.objects.create(guid=self.guid)

    def allow_resubmission(self):
        if not self.is_guid_denied:
            raise RuntimeError('GUID already denied')

        activity.log_create(amo.LOG.DENIED_GUID_DELETED, self)
        log.info('Allow resubmission for addon "%s"', self.slug)
        DeniedGuid.objects.filter(guid=self.guid).delete()

    def disable_all_files(self):
        File.objects.filter(version__addon=self).update(status=amo.STATUS_DISABLED)

    @property
    def is_guid_denied(self):
        return DeniedGuid.objects.filter(guid=self.guid).exists()

    def is_soft_deleteable(self):
        return self.status or Version.unfiltered.filter(addon=self).exists()

    def _prepare_deletion_email(self, msg, reason):
        user = core.get_user()
        # Don't localize email to admins, use 'en-US' always.
        with translation.override(settings.LANGUAGE_CODE):
            # The types are lazy translated in apps/constants/base.py.
            atype = amo.ADDON_TYPE.get(self.type, 'unknown').upper()
        context = {
            'atype': atype,
            'authors': [u.email for u in self.authors.all()],
            'adu': self.average_daily_users,
            'guid': self.guid,
            'id': self.id,
            'msg': msg,
            'reason': reason,
            'name': self.name,
            'slug': self.slug,
            'weekly_downloads': self.weekly_downloads,
            'url': jinja_helpers.absolutify(self.get_url_path()),
            'user_str': (
                '%s, %s (%s)' % (user.name, user.email, user.id) if user else 'Unknown'
            ),
        }

        email_msg = (
            """
        The following %(atype)s was deleted.
        %(atype)s: %(name)s
        URL: %(url)s
        DELETED BY: %(user_str)s
        ID: %(id)s
        GUID: %(guid)s
        AUTHORS: %(authors)s
        WEEKLY DOWNLOADS: %(weekly_downloads)s
        AVERAGE DAILY USERS: %(adu)s
        NOTES: %(msg)s
        REASON GIVEN BY USER FOR DELETION: %(reason)s
        """
            % context
        )
        log.info('Sending delete email for %(atype)s %(id)s' % context)
        subject = 'Deleting %(atype)s %(slug)s (%(id)d)' % context
        return subject, email_msg

    @transaction.atomic
    def delete(self, msg='', reason='', send_delete_email=True):
        # To avoid a circular import
        from . import tasks
        from olympia.versions import tasks as version_tasks
        from olympia.files import tasks as file_tasks

        # Check for soft deletion path. Happens only if the addon status isn't
        # 0 (STATUS_INCOMPLETE) with no versions.
        soft_deletion = self.is_soft_deleteable()
        if soft_deletion and self.status == amo.STATUS_DELETED:
            # We're already done.
            return

        id = self.id

        # Fetch previews before deleting the addon instance, so that we can
        # pass the list of files to delete to the delete_preview_files task
        # after the addon is deleted.
        previews = list(
            Preview.objects.filter(addon__id=id).values_list('id', flat=True)
        )
        version_previews = list(
            VersionPreview.objects.filter(version__addon__id=id).values_list(
                'id', flat=True
            )
        )

        if soft_deletion:
            # /!\ If we ever stop using soft deletion, and remove this code, we
            # need to make sure that the logs created below aren't cascade
            # deleted!

            log.info('Deleting add-on: %s' % self.id)

            if send_delete_email:
                email_to = [settings.DELETION_EMAIL]
                subject, email_msg = self._prepare_deletion_email(msg, reason)
            else:
                email_to, subject, email_msg = [], '', ''
            # If the add-on was disabled by Mozilla, add the guid to
            #  DeniedGuids to prevent resubmission after deletion.
            if self.status == amo.STATUS_DISABLED:
                try:
                    with transaction.atomic():
                        self.deny_resubmission()
                except RuntimeError:
                    # If the guid is already in DeniedGuids, we are good.
                    pass

            # Update or NULL out various fields.
            models.signals.pre_delete.send(sender=Addon, instance=self)
            self._ratings.all().delete()
            # We avoid triggering signals for Version & File on purpose to
            # avoid extra work. Files will be moved to the correct storage
            # location with hide_disabled_files task or hide_disabled_files
            # cron as a fallback.
            self.disable_all_files()
            file_tasks.hide_disabled_files.delay(addon_id=self.id)

            self.versions.all().update(deleted=True)
            VersionReviewerFlags.objects.filter(version__addon=self).update(
                pending_rejection=None
            )
            # The last parameter is needed to automagically create an AddonLog.
            activity.log_create(amo.LOG.DELETE_ADDON, self.pk, str(self.guid), self)
            self.update(
                status=amo.STATUS_DELETED,
                slug=None,
                _current_version=None,
                modified=datetime.now(),
            )
            models.signals.post_delete.send(sender=Addon, instance=self)

            if send_delete_email:
                send_mail(subject, email_msg, recipient_list=email_to)
        else:
            # Real deletion path.
            super(Addon, self).delete()

        for preview in previews:
            tasks.delete_preview_files.delay(preview)
        for preview in version_previews:
            version_tasks.delete_preview_files.delay(preview)

        return True

    @classmethod
    def initialize_addon_from_upload(cls, data, upload, channel, user):
        timer = StopWatch('addons.models.initialize_addon_from_upload.')
        timer.start()
        fields = [field.name for field in cls._meta.get_fields()]
        guid = data.get('guid')
        old_guid_addon = None
        if guid:  # It's an extension.
            # Reclaim GUID from deleted add-on.
            try:
                old_guid_addon = Addon.unfiltered.get(guid=guid)
                old_guid_addon.update(guid=None)
            except ObjectDoesNotExist:
                pass

        generate_guid = not data.get('guid', None) and data.get(
            'is_webextension', False
        )

        if generate_guid:
            data['guid'] = guid = generate_addon_guid()
        timer.log_interval('1.guids')

        data = cls.resolve_webext_translations(data, upload)
        timer.log_interval('2.resolve_translations')

        if channel == amo.RELEASE_CHANNEL_UNLISTED:
            data['slug'] = get_random_slug()
        timer.log_interval('3.get_random_slug')

        addon = Addon(**{k: v for k, v in data.items() if k in fields})
        timer.log_interval('4.instance_init')

        addon.status = amo.STATUS_NULL
        locale_is_set = (
            addon.default_locale
            and addon.default_locale in settings.AMO_LANGUAGES
            and data.get('default_locale') == addon.default_locale
        )
        if not locale_is_set:
            addon.default_locale = to_language(trans_real.get_language())
        timer.log_interval('5.default_locale')

        addon.save()
        timer.log_interval('6.addon_save')

        if guid:
            AddonGUID.objects.create(addon=addon, guid=guid)
        if old_guid_addon:
            old_guid_addon.update(guid=GUID_REUSE_FORMAT.format(addon.pk))
            log.info(
                f'GUID {guid} from addon [{old_guid_addon.pk}] reused '
                f'by addon [{addon.pk}].'
            )
        if user:
            AddonUser(addon=addon, user=user).save()
        timer.log_interval('7.end')
        return addon

    @classmethod
    def from_upload(
        cls,
        upload,
        selected_apps,
        channel=amo.RELEASE_CHANNEL_LISTED,
        parsed_data=None,
        user=None,
    ):
        """
        Create an Addon instance, a Version and corresponding File(s) from a
        FileUpload, a list of compatible app ids, a channel id and the
        parsed_data generated by parse_addon().

        Note that it's the caller's responsability to ensure the file is valid.
        We can't check for that here because an admin may have overridden the
        validation results.
        """
        assert parsed_data is not None

        addon = cls.initialize_addon_from_upload(parsed_data, upload, channel, user)

        Version.from_upload(
            upload=upload,
            addon=addon,
            selected_apps=selected_apps,
            channel=channel,
            parsed_data=parsed_data,
        )

        activity.log_create(amo.LOG.CREATE_ADDON, addon)
        log.info('New addon %r from %r' % (addon, upload))

        return addon

    @classmethod
    def resolve_webext_translations(cls, data, upload):
        """Resolve all possible translations from an add-on.

        This returns a modified `data` dictionary accordingly with proper
        translations filled in.
        """
        default_locale = find_language(data.get('default_locale'))

        if not data.get('is_webextension') or not default_locale:
            # Don't change anything if we don't meet the requirements
            return data

        # find_language might have expanded short to full locale, so update it.
        data['default_locale'] = default_locale

        fields = ('name', 'homepage', 'summary')
        messages = extract_translations(upload)

        for field in fields:
            data[field] = {
                locale: resolve_i18n_message(
                    data[field],
                    locale=locale,
                    default_locale=default_locale,
                    messages=messages,
                )
                for locale in messages
            }

        return data

    def get_url_path(self, add_prefix=True):
        if not self._current_version_id:
            return ''
        return reverse('addons.detail', args=[self.slug], add_prefix=add_prefix)

    def get_dev_url(self, action='edit', args=None, prefix_only=False):
        args = args or []
        prefix = 'devhub'
        if not prefix_only:
            prefix += '.addons'
        view_name = '{prefix}.{action}'.format(prefix=prefix, action=action)
        return reverse(view_name, args=[self.slug] + args)

    def get_detail_url(self, action='detail', args=None):
        if args is None:
            args = []
        return reverse('addons.%s' % action, args=[self.slug] + args)

    @property
    def ratings_url(self):
        return reverse('addons.ratings.list', args=[self.slug])

    @cached_property
    def listed_authors(self):
        return self.authors.filter(addons=self, addonuser__listed=True).order_by(
            'addonuser__position'
        )

    @classmethod
    def get_fallback(cls):
        return cls._meta.get_field('default_locale')

    @property
    def ratings(self):
        return Rating.objects.filter(addon=self, reply_to=None)

    def language_ascii(self):
        lang = trans_real.to_language(self.default_locale)
        return settings.LANGUAGES.get(lang)

    @property
    def valid_file_statuses(self):
        if self.status == amo.STATUS_APPROVED:
            return [amo.STATUS_APPROVED]
        return amo.VALID_FILE_STATUSES

    def find_latest_public_listed_version(self):
        """Retrieve the latest public listed version of an addon.

        If the add-on is not public, it can return a listed version awaiting
        review (since non-public add-ons should not have public versions)."""
        try:
            statuses = self.valid_file_statuses
            status_list = ','.join(map(str, statuses))
            fltr = {
                'channel': amo.RELEASE_CHANNEL_LISTED,
                'files__status__in': statuses,
            }
            return self.versions.filter(**fltr).extra(
                where=[
                    """
                    NOT EXISTS (
                        SELECT 1 FROM files AS f2
                        WHERE f2.version_id = versions.id AND
                              f2.status NOT IN (%s))
                    """
                    % status_list
                ]
            )[0]

        except (IndexError, Version.DoesNotExist):
            return None

    def find_latest_version(self, channel, exclude=((amo.STATUS_DISABLED,))):
        """Retrieve the latest version of an add-on for the specified channel.

        If channel is None either channel is returned.

        Keyword arguments:
        exclude -- exclude versions for which all files have one
                   of those statuses (default STATUS_DISABLED)."""

        # If the add-on is deleted or hasn't been saved yet, it should not
        # have a latest version.
        if not self.id or self.status == amo.STATUS_DELETED:
            return None
        # We can't use .exclude(files__status=excluded_statuses) because that
        # would exclude a version if *any* of its files match but if there is
        # only one file that doesn't have one of the excluded statuses it
        # should be enough for that version to be considered.
        params = {
            'files__status__in': (set(amo.STATUS_CHOICES_FILE.keys()) - set(exclude))
        }
        if channel is not None:
            params['channel'] = channel
        try:
            # Avoid most transformers - keep translations because they don't
            # get automatically fetched if you just access the field without
            # having made the query beforehand, and we don't know what callers
            # will want ; but for the rest of them, since it's a single
            # instance there is no reason to call the default transformers.
            latest_qs = self.versions.filter(**params).only_translations()
            latest = latest_qs.latest()
        except Version.DoesNotExist:
            latest = None
        return latest

    @use_primary_db
    def update_version(self, ignore=None, _signal=True):
        """
        Update the current_version field on this add-on if necessary.

        Returns True if we updated the current_version field.

        The optional ``ignore`` parameter, if present, is a a version
        to not consider as part of the update, since it may be in the
        process of being deleted.

        Pass ``_signal=False`` if you want to no signals fired at all.

        """
        new_current_version = self.find_latest_public_listed_version()
        updated = {}
        send_signal = False
        if self._current_version != new_current_version:
            updated['_current_version'] = new_current_version
            send_signal = True

        # update_version can be called by a post_delete signal (such
        # as File's) when deleting a version. If so, we should avoid putting
        # that version-being-deleted in any fields.
        if ignore is not None:
            updated = {k: v for k, v in updated.items() if v != ignore}

        if updated:
            diff = [self._current_version, new_current_version]
            # Pass along _signal to the .update() to prevent it from firing
            # signals if we don't want them.
            updated['_signal'] = _signal
            try:
                self.update(**updated)
                if send_signal and _signal:
                    signals.version_changed.send(sender=self.__class__, instance=self)
                log.info(
                    'Version changed from current: %s to %s '
                    'for addon %s' % tuple(diff + [self])
                )
            except Exception as e:
                log.error(
                    'Could not save version changes current: %s to %s '
                    'for addon %s (%s)' % tuple(diff + [self, e])
                )

        return bool(updated)

    def increment_theme_version_number(self):
        """Increment theme version number by 1."""
        latest_version = self.find_latest_version(None)
        version = latest_version or self.current_version
        version.version = str(float(version.version) + 1)
        # Set the current version.
        self.update(_current_version=version.save())

    @property
    def current_version(self):
        """Return the latest public listed version of an addon.

        If the add-on is not public, it can return a listed version awaiting
        review (since non-public add-ons should not have public versions).

        If the add-on has not been created yet or is deleted, it returns None.
        """
        if not self.id or self.status == amo.STATUS_DELETED:
            return None
        try:
            return self._current_version
        except ObjectDoesNotExist:
            pass
        return None

    @cached_property
    def latest_unlisted_version(self):
        """Shortcut property for Addon.find_latest_version(
        channel=RELEASE_CHANNEL_UNLISTED)."""
        return self.find_latest_version(channel=amo.RELEASE_CHANNEL_UNLISTED)

    @cached_property
    def binary(self):
        """Returns if the current version has binary files."""
        version = self.current_version
        if version:
            return version.files.filter(binary=True).exists()
        return False

    @cached_property
    def binary_components(self):
        """Returns if the current version has files with binary_components."""
        version = self.current_version
        if version:
            return version.files.filter(binary_components=True).exists()
        return False

    def get_icon_dir(self):
        return os.path.join(
            jinja_helpers.user_media_path('addon_icons'), '%s' % (self.id // 1000)
        )

    def get_icon_url(self, size, use_default=True):
        """
        Returns the addon's icon url according to icon_type.

        If it's a theme and there is no icon set, it will return the default
        theme icon.

        If it's something else, it will return the default add-on icon, unless
        use_default is False, in which case it will return None.
        """
        icon_type_split = []
        if self.icon_type:
            icon_type_split = self.icon_type.split('/')

        # Get the closest allowed size without going over
        if size not in amo.ADDON_ICON_SIZES and size >= amo.ADDON_ICON_SIZES[0]:
            size = [s for s in amo.ADDON_ICON_SIZES if s < size][-1]
        elif size < amo.ADDON_ICON_SIZES[0]:
            size = amo.ADDON_ICON_SIZES[0]

        # Figure out what to return for an image URL
        if not self.icon_type:
            return self.get_default_icon_url(size) if use_default else None
        elif icon_type_split[0] == 'icon':
            return '{0}img/addon-icons/{1}-{2}.png'.format(
                settings.STATIC_URL, icon_type_split[1], size
            )
        else:
            # [1] is the whole ID, [2] is the directory
            split_id = re.match(r'((\d*?)\d{1,3})$', str(self.id))
            # Use the icon hash if we have one as the cachebusting suffix,
            # otherwise fall back to the add-on modification date.
            suffix = self.icon_hash or str(int(time.mktime(self.modified.timetuple())))
            path = '/'.join(
                [
                    split_id.group(2) or '0',
                    '{0}-{1}.png?modified={2}'.format(self.id, size, suffix),
                ]
            )
            return jinja_helpers.user_media_url('addon_icons') + path

    def get_default_icon_url(self, size):
        return '{0}img/addon-icons/{1}-{2}.png'.format(
            settings.STATIC_URL, 'default', size
        )

    @use_primary_db
    def update_status(self, ignore_version=None):
        self.reload()

        if self.status in [amo.STATUS_NULL, amo.STATUS_DELETED] or self.is_disabled:
            self.update_version(ignore=ignore_version)
            return

        versions = self.versions.filter(channel=amo.RELEASE_CHANNEL_LISTED)
        status = None
        reason = ''
        if not versions.exists():
            status = amo.STATUS_NULL
            reason = 'no listed versions'
        elif not versions.filter(files__status__in=amo.VALID_FILE_STATUSES).exists():
            status = amo.STATUS_NULL
            reason = 'no listed version with valid file'
        elif (
            self.status == amo.STATUS_APPROVED
            and not versions.filter(files__status=amo.STATUS_APPROVED).exists()
        ):
            if versions.filter(files__status=amo.STATUS_AWAITING_REVIEW).exists():
                status = amo.STATUS_NOMINATED
                reason = 'only an unreviewed file'
            else:
                status = amo.STATUS_NULL
                reason = 'no reviewed files'
        elif self.status == amo.STATUS_APPROVED:
            latest_version = self.find_latest_version(
                channel=amo.RELEASE_CHANNEL_LISTED
            )
            if (
                latest_version
                and latest_version.has_files
                and (latest_version.all_files[0].status == amo.STATUS_AWAITING_REVIEW)
            ):
                # Addon is public, but its latest file is not (it's the case on
                # a new file upload). So, call update, to trigger watch_status,
                # which takes care of setting nomination time when needed.
                status = self.status
                reason = 'triggering watch_status'

        if status is not None:
            log.info(
                'Changing add-on status [%s]: %s => %s (%s).'
                % (self.id, self.status, status, reason)
            )
            self.update(status=status)
            activity.log_create(amo.LOG.CHANGE_STATUS, self, self.status)

        self.update_version(ignore=ignore_version)

    @staticmethod
    def attach_related_versions(addons, addon_dict=None):
        if addon_dict is None:
            addon_dict = {addon.id: addon for addon in addons}

        all_ids = set(filter(None, (addon._current_version_id for addon in addons)))
        versions = list(Version.objects.filter(id__in=all_ids).order_by())
        for version in versions:
            try:
                addon = addon_dict[version.addon_id]
            except KeyError:
                log.info('Version %s has an invalid add-on id.' % version.id)
                continue
            if addon._current_version_id == version.id:
                addon._current_version = version

            version.addon = addon

    @staticmethod
    def _attach_authors(
        addons,
        addon_dict=None,
        manager='objects',
        listed=True,
        to_attr='listed_authors',
    ):
        # It'd be nice if this could be done with something like
        # qs.prefetch_related(
        #     Prefetch('authors', queryset=UserProfile.objects.annotate(
        #         role=F('addonuser__role'), listed=F('addonuser__listed'))))
        # instead, but that doesn't work because the prefetch queryset is
        # making a different join for addonuser than the one used by the
        # manytomanyfield, so the results are completely wrong when there are
        # more than one add-on. Also this wouldn't let us customize the
        # AddonUser manager to include/exclude deleted roles.
        # So instead, we do it via AddonUser, copy the properties on the users
        # and throw away the AddonUser instances afterwards.
        if addon_dict is None:
            addon_dict = {addon.id: addon for addon in addons}

        filters = {'addon__in': addons}
        if listed is not None:
            filters['listed'] = listed
        addonuser_qs = getattr(AddonUser, manager).all()
        addonuser_qs = (
            addonuser_qs.filter(**filters)
            .order_by('addon_id', 'position')
            .select_related('user')
        )
        seen = set()
        groupby = itertools.groupby(addonuser_qs, key=lambda u: u.addon_id)
        for addon_id, addonusers in groupby:
            authors = []
            for addonuser in addonusers:
                setattr(addonuser.user, 'role', addonuser.role)
                setattr(addonuser.user, 'listed', addonuser.listed)
                authors.append(addonuser.user)
            setattr(addon_dict[addon_id], to_attr, authors)
            seen.add(addon_id)
        # set authors to empty list on addons without any.
        [
            setattr(addon, to_attr, [])
            for addon in addon_dict.values()
            if addon.id not in seen
        ]

    @staticmethod
    def attach_listed_authors(addons, addon_dict=None):
        Addon._attach_authors(addons, addon_dict=addon_dict)

    @staticmethod
    def attach_all_authors(addons, addon_dict=None):
        Addon._attach_authors(
            addons,
            addon_dict=addon_dict,
            manager='unfiltered',
            listed=None,
            to_attr='all_authors',
        )

    @staticmethod
    def attach_previews(addons, addon_dict=None, no_transforms=False):
        if addon_dict is None:
            addon_dict = {a.id: a for a in addons}

        qs = Preview.objects.filter(addon__in=addons, position__gte=0).order_by()
        if no_transforms:
            qs = qs.no_transforms()
        qs = sorted(qs, key=lambda x: (x.addon_id, x.position, x.created))
        seen = set()
        for addon_id, previews in itertools.groupby(qs, lambda x: x.addon_id):
            addon_dict[addon_id]._all_previews = list(previews)
            seen.add(addon_id)
        # set _all_previews to empty list on addons without previews.
        [
            setattr(addon, '_all_previews', [])
            for addon in addon_dict.values()
            if addon.id not in seen
        ]

    @staticmethod
    def attach_static_categories(addons, addon_dict=None):
        if addon_dict is None:
            addon_dict = {addon.id: addon for addon in addons}

        qs = AddonCategory.objects.filter(addon__in=addon_dict.values()).values_list(
            'addon_id', 'category_id'
        )

        for addon_id, cats_iter in itertools.groupby(qs, key=lambda x: x[0]):
            # The second value of each tuple in cats_iter are the category ids
            # we want.
            addon_dict[addon_id].category_ids = sorted([c[1] for c in cats_iter])
            addon_dict[addon_id].all_categories = [
                CATEGORIES_BY_ID[cat_id]
                for cat_id in addon_dict[addon_id].category_ids
                if cat_id in CATEGORIES_BY_ID
            ]

    @staticmethod
    @timer
    def transformer(addons):
        if not addons:
            return

        addon_dict = {a.id: a for a in addons}

        # Attach categories.
        Addon.attach_static_categories(addons, addon_dict=addon_dict)
        # Set _current_version and attach listed authors.
        Addon.attach_related_versions(addons, addon_dict=addon_dict)
        Addon.attach_listed_authors(addons, addon_dict=addon_dict)
        # Attach previews.
        Addon.attach_previews(addons, addon_dict=addon_dict)

        return addon_dict

    @property
    def contribution_url(self, lang=settings.LANGUAGE_CODE, app=settings.DEFAULT_APP):
        return reverse('addons.contribute', args=[self.slug])

    @property
    def thumbnail_url(self):
        """
        Returns the addon's thumbnail url or a default.
        """
        try:
            preview = self._all_previews[0]
            return preview.thumbnail_url
        except IndexError:
            return settings.STATIC_URL + '/img/icons/no-preview.png'

    def can_request_review(self):
        """Return whether an add-on can request a review or not."""
        if self.is_disabled or self.status in (
            amo.STATUS_APPROVED,
            amo.STATUS_NOMINATED,
            amo.STATUS_DELETED,
        ):
            return False

        latest_version = self.find_latest_version(
            amo.RELEASE_CHANNEL_LISTED, exclude=()
        )

        return (
            latest_version is not None
            and latest_version.files.exists()
            and not any(file.reviewed for file in latest_version.all_files)
        )

    @property
    def is_disabled(self):
        """True if this Addon is disabled.

        It could be disabled by an admin or disabled by the developer
        """
        return self.status == amo.STATUS_DISABLED or self.disabled_by_user

    @property
    def is_deleted(self):
        return self.status == amo.STATUS_DELETED

    def is_unreviewed(self):
        return self.status in amo.UNREVIEWED_ADDON_STATUSES

    def is_public(self):
        return self.status == amo.STATUS_APPROVED and not self.disabled_by_user

    def has_complete_metadata(self, has_listed_versions=None):
        """See get_required_metadata for has_listed_versions details."""
        return all(self.get_required_metadata(has_listed_versions=has_listed_versions))

    def get_required_metadata(self, has_listed_versions=None):
        """If has_listed_versions is not specified this method will return the
        current (required) metadata (truthy values if present) for this Addon.

        If has_listed_versions is specified then the method will act as if
        Addon.has_listed_versions() returns that value. Used to predict if the
        addon will require extra metadata before a version is created."""
        if has_listed_versions is None:
            has_listed_versions = self.has_listed_versions()
        if not has_listed_versions:
            # Add-ons with only unlisted versions have no required metadata.
            return []
        # We need to find out if the add-on has a license set. We prefer to
        # check the current_version first because that's what would be used for
        # public pages, but if there isn't any listed version will do.
        version = self.current_version or self.find_latest_version(
            channel=amo.RELEASE_CHANNEL_LISTED, exclude=()
        )
        return [
            self.all_categories,
            self.name,
            self.summary,
            (version and version.license),
        ]

    def should_redirect_to_submit_flow(self):
        return (
            self.status == amo.STATUS_NULL
            and not self.has_complete_metadata()
            and self.find_latest_version(channel=amo.RELEASE_CHANNEL_LISTED)
        )

    def can_be_deleted(self):
        return not self.is_deleted

    def has_listed_versions(self, include_deleted=False):
        if include_deleted:
            manager = self.versions(manager='unfiltered_for_relations')
        else:
            manager = self.versions
        return (
            self._current_version_id
            or manager.filter(channel=amo.RELEASE_CHANNEL_LISTED).exists()
        )

    def has_unlisted_versions(self, include_deleted=False):
        if include_deleted:
            manager = self.versions(manager='unfiltered_for_relations')
        else:
            manager = self.versions
        return manager.filter(channel=amo.RELEASE_CHANNEL_UNLISTED).exists()

    @property
    def is_restart_required(self):
        """Whether the add-on current version requires a browser restart to
        work."""
        return self.current_version and self.current_version.is_restart_required

    def _is_recommended_theme(self):
        from olympia.bandwagon.models import CollectionAddon

        return (
            self.type == amo.ADDON_STATICTHEME
            and CollectionAddon.objects.filter(
                collection_id=settings.COLLECTION_FEATURED_THEMES_ID, addon=self
            ).exists()
        )

    def promoted_group(self, *, currently_approved=True):
        """Is the addon currently promoted for the current applications?

        Returns the group constant, or NOT_PROMOTED (which is falsey)
        otherwise.

        `currently_approved=True` means only returns True if
        self.current_version is approved for the current promotion & apps.
        If currently_approved=False then promotions where there isn't approval
        are returned too.
        """
        from olympia.promoted.models import PromotedAddon

        try:
            promoted = self.promotedaddon
        except PromotedAddon.DoesNotExist:
            return NOT_PROMOTED
        is_promoted = not currently_approved or promoted.approved_applications
        return promoted.group if is_promoted else NOT_PROMOTED

    @cached_property
    def promoted(self):
        promoted_group = self.promoted_group()
        if promoted_group:
            return self.promotedaddon
        else:
            from olympia.promoted.models import PromotedTheme

            if self._is_recommended_theme():
                return PromotedTheme(addon=self, group_id=RECOMMENDED.id)
        return None

    @property
    def promoted_subscription(self):
        """Returns a PromotedSubscription if it exists, None otherwise."""
        from olympia.promoted.models import PromotedAddon, PromotedSubscription

        try:
            return self.promotedaddon.promotedsubscription
        except (PromotedAddon.DoesNotExit, PromotedSubscription.DoesNotExist):
            pass
        return None

    @cached_property
    def tags_partitioned_by_developer(self):
        """Returns a tuple of developer tags and user tags for this addon."""
        tags = self.tags.not_denied()
        user_tags = tags.exclude(addon_tags__user__in=self.listed_authors)
        dev_tags = tags.exclude(id__in=[t.id for t in user_tags])
        return dev_tags, user_tags

    @cached_property
    def compatible_apps(self):
        """Shortcut to get compatible apps for the current version."""
        if self.current_version:
            return self.current_version.compatible_apps
        else:
            return {}

    def accepts_compatible_apps(self):
        """True if this add-on lists compatible apps."""
        return self.type not in amo.NO_COMPAT

    def incompatible_latest_apps(self):
        """Returns a list of applications with which this add-on is
        incompatible (based on the latest version of each app).
        """
        apps = []

        for application, version in self.compatible_apps.items():
            if not version:
                continue

            latest_version = version.get_latest_application_version()

            if version_int(version.max.version) < version_int(latest_version):
                apps.append((application, latest_version))
        return apps

    def has_author(self, user):
        """True if ``user`` is an author of the add-on."""
        if user is None or user.is_anonymous:
            return False
        return AddonUser.objects.filter(addon=self, user=user).exists()

    @classmethod
    def _last_updated_queries(cls):
        """
        Get the queries used to calculate addon.last_updated.
        """
        status_change = Max('versions__files__datestatuschanged')
        public = (
            Addon.objects.filter(
                status=amo.STATUS_APPROVED, versions__files__status=amo.STATUS_APPROVED
            )
            .values('id')
            .annotate(last_updated=status_change)
        )

        stati = amo.VALID_ADDON_STATUSES
        exp = (
            Addon.objects.exclude(status__in=stati)
            .filter(versions__files__status__in=amo.VALID_FILE_STATUSES)
            .values('id')
            .annotate(last_updated=Max('versions__files__created'))
        )

        return {'public': public, 'exp': exp}

    @cached_property
    def all_categories(self):
        return [addoncat.category for addoncat in self.addoncategory_set.all()]

    @cached_property
    def current_previews(self):
        """Previews for the current version, or all of them if not a
        static theme."""
        if self.has_per_version_previews:
            if self.current_version:
                return self.current_version.previews.all()
            return VersionPreview.objects.none()
        else:
            return self._all_previews

    @cached_property
    def _all_previews(self):
        """Exclude promo graphics."""
        return list(self.previews.exclude(position=-1))

    @property
    def has_per_version_previews(self):
        return self.type == amo.ADDON_STATICTHEME

    @property
    def app_categories(self):
        app_cats = {}
        categories = sorted_groupby(
            sorted(self.all_categories),
            key=lambda x: getattr(amo.APP_IDS.get(x.application), 'short', ''),
        )
        for app, cats in categories:
            app_cats[app] = list(cats)
        return app_cats

    def remove_locale(self, locale):
        """NULLify strings in this locale for the add-on and versions."""
        for o in itertools.chain([self], self.versions.all()):
            Translation.objects.remove_for(o, locale)

    def check_ownership(
        self, request, require_owner, require_author, ignore_disabled, admin
    ):
        """
        Used by acl.check_ownership to see if request.user has permissions for
        the addon.
        """
        if require_author:
            require_owner = False
            ignore_disabled = True
            admin = False
        return acl.check_addon_ownership(
            request,
            self,
            admin=admin,
            dev=(not require_owner),
            ignore_disabled=ignore_disabled,
        )

    def should_show_permissions(self, version=None):
        version = version or self.current_version
        return (
            self.type == amo.ADDON_EXTENSION
            and version
            and version.all_files[0]
            and (
                not version.all_files[0].is_webextension
                or version.all_files[0].permissions
                or version.all_files[0].optional_permissions
            )
        )

    # Aliases for reviewerflags below are not just useful in case
    # AddonReviewerFlags does not exist for this add-on: they are also used
    # by reviewer tools get_flags() function to return flags shown to reviewers
    # in both the review queues and the review page.

    @property
    def needs_admin_code_review(self):
        try:
            return self.reviewerflags.needs_admin_code_review
        except AddonReviewerFlags.DoesNotExist:
            return None

    @property
    def needs_admin_content_review(self):
        try:
            return self.reviewerflags.needs_admin_content_review
        except AddonReviewerFlags.DoesNotExist:
            return None

    @property
    def needs_admin_theme_review(self):
        try:
            return self.reviewerflags.needs_admin_theme_review
        except AddonReviewerFlags.DoesNotExist:
            return None

    @property
    def auto_approval_disabled(self):
        try:
            return self.reviewerflags.auto_approval_disabled
        except AddonReviewerFlags.DoesNotExist:
            return None

    @property
    def auto_approval_disabled_until_next_approval(self):
        try:
            return self.reviewerflags.auto_approval_disabled_until_next_approval
        except AddonReviewerFlags.DoesNotExist:
            return None

    @property
    def auto_approval_delayed_until(self):
        try:
            return self.reviewerflags.auto_approval_delayed_until
        except AddonReviewerFlags.DoesNotExist:
            return None

    @property
    def auto_approval_delayed_indefinitely(self):
        return self.auto_approval_delayed_until == datetime.max

    @property
    def auto_approval_delayed_temporarily(self):
        return (
            bool(self.auto_approval_delayed_until)
            and self.auto_approval_delayed_until != datetime.max
            and self.auto_approval_delayed_until > datetime.now()
        )

    def reset_notified_about_auto_approval_delay(self):
        """
        Reset notified_about_auto_approval_delay reviewer flag for this addon.

        This doesn't create an AddonReviewerFlags if there wasn't one, just
        resets notified_about_auto_approval_delay to False if there were flags
        for this add-on.
        """
        AddonReviewerFlags.objects.filter(addon=self).update(
            notified_about_auto_approval_delay=False
        )

    @classmethod
    def get_lookup_field(cls, identifier):
        lookup_field = 'pk'
        if identifier and not identifier.isdigit():
            # If the identifier contains anything other than a digit, it's
            # either a slug or a guid. guids need to contain either {} or @,
            # which are invalid in a slug.
            if amo.ADDON_GUID_PATTERN.match(identifier):
                lookup_field = 'guid'
            else:
                lookup_field = 'slug'
        return lookup_field

    @property
    def addonguid_guid(self):
        """Use this function to avoid having to wrap `addon.addonguid.guid` in
        a try...except.
        There *should* be a matching AddonGUID record for every Addon with a
        guid, but the foreign key is from AddonGUID to Addon so there's a
        possiblity of bad data leading to the AddonGUID not existing.  Plus we
        don't want this to fail if an upload with guid=None somehow ended up
        getting through.
        """
        return getattr(self, 'addonguid', self).guid

    @cached_property
    def block(self):
        from olympia.blocklist.models import Block

        # Block.guid is unique so it's either on the list or not.
        return Block.objects.filter(guid=self.addonguid_guid).last()

    @cached_property
    def blocklistsubmission(self):
        from olympia.blocklist.models import BlocklistSubmission

        # GUIDs should only exist in one (active) submission at once.
        return BlocklistSubmission.get_submissions_from_guid(self.addonguid_guid).last()

    @property
    def git_extraction_is_in_progress(self):
        from olympia.git.models import GitExtractionEntry

        return GitExtractionEntry.objects.filter(addon=self, in_progress=True).exists()


dbsignals.pre_save.connect(save_signal, sender=Addon, dispatch_uid='addon_translations')


@receiver(signals.version_changed, dispatch_uid='version_changed')
def version_changed(sender, instance, **kw):
    from . import tasks
    from olympia.amo.tasks import trigger_sync_objects_to_basket

    # watch_changes() also does a sync when it detects a _current_version change, but it
    # might not have fired, since it depends on on_change() being sent.
    trigger_sync_objects_to_basket('addon', [instance.pk], 'version change')
    tasks.version_changed.delay(instance.pk)


@receiver(dbsignals.post_save, sender=Addon, dispatch_uid='addons.search.index')
def update_search_index(sender, instance, **kw):
    from . import tasks

    if not kw.get('raw'):
        tasks.index_addons.delay([instance.id])


@Addon.on_change
def watch_status(old_attr=None, new_attr=None, instance=None, sender=None, **kwargs):
    """
    Set nomination date if the addon is new in queue or updating.

    The nomination date cannot be reset, say, when a developer cancels
    their request for review and re-requests review.

    If a version is rejected after nomination, the developer has
    to upload a new version.

    """
    if old_attr is None:
        old_attr = {}
    if new_attr is None:
        new_attr = {}
    new_status = new_attr.get('status')
    old_status = old_attr.get('status')
    latest_version = instance.find_latest_version(channel=amo.RELEASE_CHANNEL_LISTED)

    # Update the author's account profile visibility
    if new_status != old_status:
        [author.update_is_public() for author in instance.authors.all()]

    if (
        new_status not in amo.VALID_ADDON_STATUSES
        or not new_status
        or not latest_version
    ):
        return

    if old_status not in amo.UNREVIEWED_ADDON_STATUSES:
        # New: will (re)set nomination only if it's None.
        latest_version.reset_nomination_time()
    elif latest_version.has_files:
        # Updating: inherit nomination from last nominated version.
        # Calls `inherit_nomination` manually given that signals are
        # deactivated to avoid circular calls.
        inherit_nomination(None, latest_version)


@Addon.on_change
def watch_disabled(old_attr=None, new_attr=None, instance=None, sender=None, **kwargs):
    """
    Move files when an add-on is disabled/enabled.

    There is a similar watcher in olympia.files.models that tracks File
    status, but this one is useful for when the Files do not change their
    status.
    """
    if old_attr is None:
        old_attr = {}
    if new_attr is None:
        new_attr = {}
    attrs = {
        key: value
        for key, value in old_attr.items()
        if key in ('disabled_by_user', 'status')
    }
    was_disabled = Addon(**attrs).is_disabled
    is_disabled = instance.is_disabled
    if was_disabled and not is_disabled:
        for file_ in File.objects.filter(version__addon=instance.id):
            file_.unhide_disabled_file()
    elif is_disabled and not was_disabled:
        for file_ in File.objects.filter(version__addon=instance.id):
            file_.hide_disabled_file()


@Addon.on_change
def watch_changes(old_attr=None, new_attr=None, instance=None, sender=None, **kwargs):
    if old_attr is None:
        old_attr = {}
    if new_attr is None:
        new_attr = {}

    changes = {
        x for x in new_attr if not x.startswith('_') and new_attr[x] != old_attr.get(x)
    }
    basket_relevant_changes = (
        # Some changes are not tracked here:
        # - Any authors changes (separate model)
        # - Creation/Deletion of unlisted version (separate model)
        # - Name change (separate model/signal, see below)
        # - Categories changes (separate model, ignored for now)
        # - average_rating changes (ignored for now, happens too often)
        # - average_daily_users changes (ignored for now, happens too often)
        '_current_version',
        'default_locale',
        'slug',
        'status',
        'disabled_by_user',
    )
    if any(field in changes for field in basket_relevant_changes):
        from olympia.amo.tasks import trigger_sync_objects_to_basket

        trigger_sync_objects_to_basket('addon', [instance.pk], 'attribute change')


@receiver(translation_saved, sender=Addon, dispatch_uid='watch_addon_name_changes')
def watch_addon_name_changes(sender=None, instance=None, **kw):
    field_name = kw.get('field_name')
    if instance and field_name == 'name':
        from olympia.amo.tasks import trigger_sync_objects_to_basket

        trigger_sync_objects_to_basket('addon', [instance.pk], 'name change')


def attach_translations_dict(addons):
    """Put all translations into a translations dict."""
    attach_trans_dict(Addon, addons)


def attach_tags(addons):
    addon_dict = {addon.id: addon for addon in addons}
    for addon in addons:
        addon.tag_list = []  # make sure all the addons have the property set
    qs = (
        Tag.objects.not_denied()
        .filter(addons__in=addon_dict)
        .values_list('addons__id', 'tag_text')
    )
    for addon, tags in sorted_groupby(qs, lambda x: x[0]):
        addon_dict[addon].tag_list = [t[1] for t in tags]


class AddonReviewerFlags(ModelBase):
    addon = models.OneToOneField(
        Addon, primary_key=True, on_delete=models.CASCADE, related_name='reviewerflags'
    )
    needs_admin_code_review = models.BooleanField(default=False)
    needs_admin_content_review = models.BooleanField(default=False)
    needs_admin_theme_review = models.BooleanField(default=False)
    auto_approval_disabled = models.BooleanField(default=False)
    auto_approval_disabled_until_next_approval = models.NullBooleanField(default=None)
    auto_approval_delayed_until = models.DateTimeField(default=None, null=True)
    notified_about_auto_approval_delay = models.NullBooleanField(default=None)
    notified_about_expiring_delayed_rejections = models.NullBooleanField(default=None)


class AddonRegionalRestrictions(ModelBase):
    addon = models.OneToOneField(
        Addon,
        primary_key=True,
        on_delete=models.CASCADE,
        related_name='regional_restrictions',
        help_text='Add-on id this item will point to.',
    )
    excluded_regions = JSONField(
        default=list,
        help_text='JSON style list of ISO 3166-1 alpha-2 country (region) '
        'codes. Codes will be uppercased. E.g. `["CN"]`',
    )

    class Meta:
        verbose_name_plural = 'Addon Regional Restrictions'

    def __str__(self):
        return '%s: %d' % (self.addon, len(self.excluded_regions))

    def clean(self):
        super().clean()
        self.excluded_regions = [str(item).upper() for item in self.excluded_regions]


class MigratedLWT(OnChangeMixin, ModelBase):
    lightweight_theme_id = models.PositiveIntegerField()
    getpersonas_id = models.PositiveIntegerField()
    static_theme = models.ForeignKey(
        Addon, unique=True, related_name='migrated_from_lwt', on_delete=models.CASCADE
    )

    class Meta:
        db_table = 'migrated_personas'
        indexes = [
            LongNameIndex(
                fields=('static_theme',),
                name='migrated_personas_static_theme_id_fk_addons_id',
            ),
            LongNameIndex(
                fields=('getpersonas_id',), name='migrated_personas_getpersonas_id'
            ),
        ]


class AddonCategory(models.Model):
    id = PositiveAutoField(primary_key=True)
    addon = models.ForeignKey(Addon, on_delete=models.CASCADE)
    category_id = models.PositiveIntegerField()

    class Meta:
        db_table = 'addons_categories'
        indexes = [
            models.Index(fields=('category_id', 'addon'), name='category_addon_idx'),
        ]
        constraints = [
            models.UniqueConstraint(fields=('addon', 'category_id'), name='addon_id'),
        ]

    def __init__(self, *args, **kwargs):
        if 'category' in kwargs:
            kwargs['category_id'] = kwargs.pop('category').id
        super().__init__(*args, **kwargs)

    @property
    def category(self):
        return CATEGORIES_BY_ID.get(self.category_id)


class AddonUserManager(ManagerBase):
    def __init__(self, include_deleted=False):
        # DO NOT change the default value of include_deleted unless you've read
        # through the comment just above the Addon managers
        # declaration/instantiation and understand the consequences.
        super().__init__()
        self.include_deleted = include_deleted

    def get_queryset(self):
        qs = super().get_queryset()
        if not self.include_deleted:
            qs = qs.exclude(role=amo.AUTHOR_ROLE_DELETED)
        return qs


class AddonUser(OnChangeMixin, SaveUpdateMixin, models.Model):
    id = PositiveAutoField(primary_key=True)
    addon = models.ForeignKey(Addon, on_delete=models.CASCADE)
    user = user = models.ForeignKey(UserProfile, on_delete=models.CASCADE)
    role = models.SmallIntegerField(
        default=amo.AUTHOR_ROLE_OWNER, choices=amo.AUTHOR_CHOICES_UNFILTERED
    )
    listed = models.BooleanField(_('Listed'), default=True)
    position = models.IntegerField(default=0)

    unfiltered = AddonUserManager(include_deleted=True)
    objects = AddonUserManager()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._original_role = self.role

    class Meta:
        # see Addon.Meta for details of why this base_manager_name is important
        base_manager_name = 'unfiltered'
        db_table = 'addons_users'
        indexes = [
            models.Index(fields=('listed',), name='listed'),
            models.Index(
                fields=('addon', 'user', 'listed'), name='addon_user_listed_idx'
            ),
            models.Index(fields=('addon', 'listed'), name='addon_listed_idx'),
        ]
        constraints = [
            models.UniqueConstraint(fields=('addon', 'user'), name='addon_id'),
        ]

    def delete(self):
        # soft-delete
        self.update(role=amo.AUTHOR_ROLE_DELETED)

    @property
    def is_deleted(self):
        return self.role == amo.AUTHOR_ROLE_DELETED


@AddonUser.on_change
def watch_addon_user(
    old_attr=None, new_attr=None, instance=None, sender=None, **kwargs
):
    instance.user.update_is_public()
    # Update ES because authors is included.
    update_search_index(sender=sender, instance=instance.addon, **kwargs)


def addon_user_sync(sender=None, instance=None, **kwargs):
    # Basket doesn't care what role authors have or whether they are listed
    # or not, it just needs to be updated whenever an author is added/removed.
    created_or_deleted = kwargs.get('created', True) or instance.is_deleted
    if created_or_deleted and instance.addon.status != amo.STATUS_DELETED:
        from olympia.amo.tasks import trigger_sync_objects_to_basket

        trigger_sync_objects_to_basket('addon', [instance.addon.pk], 'addonuser change')


models.signals.post_delete.connect(
    watch_addon_user, sender=AddonUser, dispatch_uid='delete_addon_user'
)
models.signals.post_delete.connect(
    addon_user_sync, sender=AddonUser, dispatch_uid='delete_addon_user_sync'
)


models.signals.post_save.connect(
    addon_user_sync, sender=AddonUser, dispatch_uid='save_addon_user_sync'
)


class AddonUserPendingConfirmation(SaveUpdateMixin, models.Model):
    id = PositiveAutoField(primary_key=True)
    addon = models.ForeignKey(Addon, on_delete=models.CASCADE)
    user = user = models.ForeignKey(UserProfile, on_delete=models.CASCADE)
    role = models.SmallIntegerField(
        default=amo.AUTHOR_ROLE_OWNER, choices=amo.AUTHOR_CHOICES
    )
    listed = models.BooleanField(_('Listed'), default=True)
    # Note: we don't bother with position for authors waiting confirmation,
    # because it's impossible to properly reconcile it with the confirmed
    # authors. Instead, authors waiting confirmation are displayed in the order
    # they have been added, and when they are confirmed they end up in the
    # last position by default.

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._original_role = self.role

    class Meta:
        db_table = 'addons_users_pending_confirmation'
        constraints = [
            models.UniqueConstraint(
                fields=('addon', 'user'),
                name='addons_users_pending_confirmation_'
                'addon_id_user_id_38e3bb32_uniq',
            ),
        ]


class AddonApprovalsCounter(ModelBase):
    """Model holding a counter of the number of times a listed version
    belonging to an add-on has been approved by a human. Reset everytime a
    listed version is auto-approved for this add-on.

    Holds 2 additional date fields:
    - last_human_review, the date of the last time a human fully reviewed the
      add-on
    - last_content_review, the date of the last time a human fully reviewed the
      add-on content (not code).
    """

    addon = models.OneToOneField(Addon, primary_key=True, on_delete=models.CASCADE)
    counter = models.PositiveIntegerField(default=0)
    last_human_review = models.DateTimeField(null=True)
    last_content_review = models.DateTimeField(null=True, db_index=True)

    def __str__(self):
        return '%s: %d' % (str(self.pk), self.counter) if self.pk else ''

    @classmethod
    def increment_for_addon(cls, addon):
        """
        Increment approval counter for the specified addon, setting the last
        human review date and last content review date to now.
        If an AddonApprovalsCounter already exists, it updates it, otherwise it
        creates and saves a new instance.
        """
        now = datetime.now()
        data = {
            'counter': 1,
            'last_human_review': now,
            'last_content_review': now,
        }
        obj, created = cls.objects.get_or_create(addon=addon, defaults=data)
        if not created:
            data['counter'] = F('counter') + 1
            obj.update(**data)
        return obj

    @classmethod
    def reset_for_addon(cls, addon):
        """
        Reset the approval counter (but not the dates) for the specified addon.
        """
        obj, created = cls.objects.update_or_create(
            addon=addon, defaults={'counter': 0}
        )
        return obj

    @classmethod
    def approve_content_for_addon(cls, addon, now=None):
        """
        Set last_content_review for this addon.
        """
        if now is None:
            now = datetime.now()
        return cls.reset_content_for_addon(addon, reset_to=now)

    @classmethod
    def reset_content_for_addon(cls, addon, reset_to=None):
        """
        Reset the last_content_review date for this addon so it triggers
        another review.
        """
        obj, created = cls.objects.update_or_create(
            addon=addon, defaults={'last_content_review': reset_to}
        )
        return obj


class DeniedGuid(ModelBase):
    id = PositiveAutoField(primary_key=True)
    guid = models.CharField(max_length=255, unique=True)
    comments = models.TextField(default='', blank=True)

    class Meta:
        db_table = 'denied_guids'

    def __str__(self):
        return self.guid


class Preview(BasePreview, ModelBase):
    id = PositiveAutoField(primary_key=True)
    addon = models.ForeignKey(Addon, related_name='previews', on_delete=models.CASCADE)
    caption = TranslatedField()
    position = models.IntegerField(default=0)
    sizes = JSONField(default=dict)

    class Meta:
        db_table = 'previews'
        ordering = ('position', 'created')
        indexes = [
            models.Index(fields=('addon',), name='addon_id'),
            models.Index(
                fields=('addon', 'position', 'created'),
                name='addon_position_created_idx',
            ),
        ]


dbsignals.pre_save.connect(
    save_signal, sender=Preview, dispatch_uid='preview_translations'
)


models.signals.post_delete.connect(
    Preview.delete_preview_files, sender=Preview, dispatch_uid='delete_preview_files'
)


class DeniedSlug(ModelBase):
    name = models.CharField(max_length=255, unique=True, default='')

    class Meta:
        db_table = 'addons_denied_slug'

    def __str__(self):
        return self.name

    @classmethod
    def blocked(cls, slug):
        return slug.isdigit() or cls.objects.filter(name=slug).exists()


class FrozenAddon(models.Model):
    """Add-ons in this table never get a hotness score."""

    id = PositiveAutoField(primary_key=True)
    addon = models.ForeignKey(Addon, on_delete=models.CASCADE)

    class Meta:
        db_table = 'frozen_addons'

    def __str__(self):
        return 'Frozen: %s' % self.addon_id


@receiver(dbsignals.post_save, sender=FrozenAddon)
def freezer(sender, instance, **kw):
    # Adjust the hotness of the FrozenAddon.
    if instance.addon_id:
        Addon.objects.get(id=instance.addon_id).update(hotness=0)


class ReplacementAddon(ModelBase):
    guid = models.CharField(max_length=255, unique=True, null=True)
    path = models.CharField(
        max_length=255,
        null=True,
        help_text=_('Addon and collection paths need to end with "/"'),
    )

    class Meta:
        db_table = 'replacement_addons'

    @staticmethod
    def path_is_external(path):
        return urlsplit(path).scheme in ['http', 'https']

    def has_external_url(self):
        return self.path_is_external(self.path)


def track_new_status(sender, instance, *args, **kw):
    if kw.get('raw'):
        # The addon is being loaded from a fixure.
        return
    if kw.get('created'):
        track_addon_status_change(instance)


models.signals.post_save.connect(
    track_new_status, sender=Addon, dispatch_uid='track_new_addon_status'
)


@Addon.on_change
def track_status_change(old_attr=None, new_attr=None, **kw):
    if old_attr is None:
        old_attr = {}
    if new_attr is None:
        new_attr = {}
    new_status = new_attr.get('status')
    old_status = old_attr.get('status')
    if new_status != old_status:
        track_addon_status_change(kw['instance'])


def track_addon_status_change(addon):
    statsd.incr('addon_status_change.all.status_{}'.format(addon.status))


class AddonGUID(ModelBase):
    """
    Addons + guids will be added to this table whenever an addon is created.
    For deleted addons it will contain an fk to the Addon instance even after
    Addon.guid has been set to null (i.e. when it's been reuploaded).
    """

    guid = models.CharField(max_length=255, null=False, db_index=True)
    addon = models.OneToOneField(
        Addon, null=False, on_delete=models.CASCADE, unique=True
    )
    hashed_guid = models.CharField(max_length=64, null=False)

    class Meta:
        db_table = 'addons_reusedguid'

    def save(self, *args, **kwargs):
        self.hashed_guid = hashlib.sha256(self.guid.encode()).hexdigest()
        super().save(*args, **kwargs)
