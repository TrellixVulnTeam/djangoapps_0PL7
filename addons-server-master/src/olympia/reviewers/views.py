import functools
import json
import time

from collections import OrderedDict
from datetime import date, datetime, timedelta

from django import http
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Prefetch, Q
from django.db.transaction import non_atomic_requests
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.http import urlquote
from django.utils.translation import ugettext
from django.views.decorators.cache import never_cache

import pygit2

from csp.decorators import csp as set_csp
from rest_framework import status
from rest_framework.decorators import action as drf_action
from rest_framework.exceptions import NotFound
from rest_framework.generics import ListAPIView
from rest_framework.mixins import (
    CreateModelMixin,
    DestroyModelMixin,
    ListModelMixin,
    RetrieveModelMixin,
    UpdateModelMixin,
)
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

import olympia.core.logger

from olympia import amo
from olympia.abuse.models import AbuseReport
from olympia.access import acl
from olympia.accounts.views import API_TOKEN_COOKIE
from olympia.activity.models import ActivityLog, CommentLog, DraftComment
from olympia.addons.decorators import addon_view, owner_or_unlisted_reviewer
from olympia.addons.models import (
    Addon,
    AddonApprovalsCounter,
    AddonReviewerFlags,
    AddonGUID,
)
from olympia.amo.decorators import (
    json_view,
    login_required,
    permission_required,
    post_required,
)
from olympia.amo.urlresolvers import reverse
from olympia.amo.utils import paginate, render
from olympia.api.permissions import (
    AllowAnyKindOfReviewer,
    AllowReviewer,
    AllowReviewerUnlisted,
    AnyOf,
    GroupPermission,
)
from olympia.constants.promoted import RECOMMENDED
from olympia.constants.reviewers import REVIEWS_PER_PAGE, REVIEWS_PER_PAGE_MAX
from olympia.devhub import tasks as devhub_tasks
from olympia.files.models import File
from olympia.ratings.models import Rating, RatingFlag
from olympia.reviewers.forms import (
    AllAddonSearchForm,
    MOTDForm,
    PublicWhiteboardForm,
    QueueSearchForm,
    RatingFlagFormSet,
    RatingModerationLogForm,
    ReviewForm,
    ReviewLogForm,
    WhiteboardForm,
)
from olympia.reviewers.models import (
    AutoApprovalSummary,
    CannedResponse,
    PerformanceGraph,
    ReviewerScore,
    ReviewerSubscription,
    ViewExtensionQueue,
    ViewRecommendedQueue,
    ViewThemeFullReviewQueue,
    ViewThemePendingQueue,
    Whiteboard,
    clear_reviewing_cache,
    get_flags,
    get_reviewing_cache,
    get_reviewing_cache_key,
    set_reviewing_cache,
)
from olympia.reviewers.serializers import (
    AddonBrowseVersionSerializer,
    AddonBrowseVersionSerializerFileOnly,
    AddonCompareVersionSerializer,
    AddonCompareVersionSerializerFileOnly,
    AddonReviewerFlagsSerializer,
    CannedResponseSerializer,
    DiffableVersionSerializer,
    DraftCommentSerializer,
    FileInfoSerializer,
)
from olympia.reviewers.utils import (
    AutoApprovedTable,
    ContentReviewTable,
    MadReviewTable,
    PendingRejectionTable,
    ReviewHelper,
    ScannersReviewTable,
    ViewUnlistedAllListTable,
    view_table_factory,
)
from olympia.scanners.models import ScannerResult
from olympia.users.models import UserProfile
from olympia.versions.models import Version, VersionReviewerFlags
from olympia.zadmin.models import get_config, set_config

from .decorators import (
    any_reviewer_or_moderator_required,
    any_reviewer_required,
    permission_or_tools_view_required,
    unlisted_addons_reviewer_required,
)


def reviewer_addon_view_factory(f):
    decorator = functools.partial(
        addon_view, qs=Addon.unfiltered.all, include_deleted_when_checking_versions=True
    )
    return decorator(f)


def context(**kw):
    ctx = {'motd': get_config('reviewers_review_motd')}
    ctx.update(kw)
    return ctx


@permission_or_tools_view_required(amo.permissions.RATINGS_MODERATE)
def ratings_moderation_log(request):
    form = RatingModerationLogForm(request.GET)
    mod_log = ActivityLog.objects.moderation_events()

    if form.is_valid():
        if form.cleaned_data['start']:
            mod_log = mod_log.filter(created__gte=form.cleaned_data['start'])
        if form.cleaned_data['end']:
            mod_log = mod_log.filter(created__lt=form.cleaned_data['end'])
        if form.cleaned_data['filter']:
            mod_log = mod_log.filter(action=form.cleaned_data['filter'].id)

    pager = paginate(request, mod_log, 50)

    data = context(form=form, pager=pager)

    return render(request, 'reviewers/moderationlog.html', data)


@permission_or_tools_view_required(amo.permissions.RATINGS_MODERATE)
def ratings_moderation_log_detail(request, id):
    log = get_object_or_404(ActivityLog.objects.moderation_events(), pk=id)

    review = None
    # I really cannot express the depth of the insanity incarnate in
    # our logging code...
    if len(log.arguments) > 1 and isinstance(log.arguments[1], Rating):
        review = log.arguments[1]

    is_admin = acl.action_allowed(request, amo.permissions.REVIEWS_ADMIN)

    can_undelete = (
        review and review.deleted and (is_admin or request.user.pk == log.user.pk)
    )

    if request.method == 'POST':
        # A Form seems overkill for this.
        if request.POST['action'] == 'undelete':
            if not can_undelete:
                raise PermissionDenied

            ReviewerScore.award_moderation_points(
                log.user, review.addon, review.id, undo=True
            )
            review.undelete()
        return redirect('reviewers.ratings_moderation_log.detail', id)

    data = context(log=log, can_undelete=can_undelete)
    return render(request, 'reviewers/moderationlog_detail.html', data)


@any_reviewer_or_moderator_required
def dashboard(request):
    # The dashboard is divided into sections that depend on what the reviewer
    # has access to, each section having one or more links, each link being
    # defined by a text and an URL. The template will show every link of every
    # section we provide in the context.
    sections = OrderedDict()
    view_all = acl.action_allowed(request, amo.permissions.REVIEWER_TOOLS_VIEW)
    admin_reviewer = is_admin_reviewer(request)
    queue_counts = fetch_queue_counts(admin_reviewer=admin_reviewer)

    if view_all or acl.action_allowed(request, amo.permissions.ADDONS_REVIEW):
        sections[ugettext('Pre-Review Add-ons')] = []
        if acl.action_allowed(request, amo.permissions.ADDONS_RECOMMENDED_REVIEW):
            sections[ugettext('Pre-Review Add-ons')].append(
                (
                    ugettext('Recommended ({0})').format(queue_counts['recommended']),
                    reverse('reviewers.queue_recommended'),
                )
            )
        sections[ugettext('Pre-Review Add-ons')].extend(
            (
                (
                    ugettext('Other Pending Review ({0})').format(
                        queue_counts['extension']
                    ),
                    reverse('reviewers.queue_extension'),
                ),
                (ugettext('Performance'), reverse('reviewers.performance')),
                (ugettext('Review Log'), reverse('reviewers.reviewlog')),
                (
                    ugettext('Add-on Review Guide'),
                    'https://wiki.mozilla.org/Add-ons/Reviewers/Guide',
                ),
            )
        )
        sections[ugettext('Security Scanners')] = [
            (
                ugettext('Flagged By Scanners'),
                reverse('reviewers.queue_scanners'),
            ),
            (
                ugettext('Flagged for Human Review'),
                reverse('reviewers.queue_mad'),
            ),
        ]

        sections[ugettext('Auto-Approved Add-ons')] = [
            (
                ugettext('Auto Approved Add-ons ({0})').format(
                    queue_counts['auto_approved']
                ),
                reverse('reviewers.queue_auto_approved'),
            ),
            (ugettext('Performance'), reverse('reviewers.performance')),
            (ugettext('Add-on Review Log'), reverse('reviewers.reviewlog')),
            (
                ugettext('Review Guide'),
                'https://wiki.mozilla.org/Add-ons/Reviewers/Guide',
            ),
        ]
    if view_all or acl.action_allowed(request, amo.permissions.ADDONS_CONTENT_REVIEW):
        sections[ugettext('Content Review')] = [
            (
                ugettext('Content Review ({0})').format(queue_counts['content_review']),
                reverse('reviewers.queue_content_review'),
            ),
            (ugettext('Performance'), reverse('reviewers.performance')),
        ]
    if view_all or acl.action_allowed(request, amo.permissions.STATIC_THEMES_REVIEW):
        sections[ugettext('Themes')] = [
            (
                ugettext('New ({0})').format(queue_counts['theme_nominated']),
                reverse('reviewers.queue_theme_nominated'),
            ),
            (
                ugettext('Updates ({0})').format(queue_counts['theme_pending']),
                reverse('reviewers.queue_theme_pending'),
            ),
            (ugettext('Performance'), reverse('reviewers.performance')),
            (ugettext('Review Log'), reverse('reviewers.reviewlog')),
            (
                ugettext('Theme Review Guide'),
                'https://wiki.mozilla.org/Add-ons/Reviewers/Themes/Guidelines',
            ),
        ]
    if view_all or acl.action_allowed(request, amo.permissions.RATINGS_MODERATE):
        sections[ugettext('User Ratings Moderation')] = [
            (
                ugettext('Ratings Awaiting Moderation ({0})').format(
                    queue_counts['moderated']
                ),
                reverse('reviewers.queue_moderated'),
            ),
            (
                ugettext('Moderated Review Log'),
                reverse('reviewers.ratings_moderation_log'),
            ),
            (
                ugettext('Moderation Guide'),
                'https://wiki.mozilla.org/Add-ons/Reviewers/Guide/Moderation',
            ),
        ]
    if view_all or acl.action_allowed(request, amo.permissions.ADDONS_REVIEW_UNLISTED):
        sections[ugettext('Unlisted Add-ons')] = [
            (ugettext('All Unlisted Add-ons'), reverse('reviewers.unlisted_queue_all')),
            (
                ugettext('Review Guide'),
                'https://wiki.mozilla.org/Add-ons/Reviewers/Guide',
            ),
        ]
    if view_all or acl.action_allowed(
        request, amo.permissions.ADDON_REVIEWER_MOTD_EDIT
    ):
        sections[ugettext('Announcement')] = [
            (ugettext('Update message of the day'), reverse('reviewers.motd'))
        ]
    if view_all or acl.action_allowed(request, amo.permissions.REVIEWS_ADMIN):
        sections[ugettext('Admin Tools')] = [
            (
                ugettext('Add-ons Pending Rejection ({0})').format(
                    queue_counts['pending_rejection']
                ),
                reverse('reviewers.queue_pending_rejection'),
            )
        ]
    return render(
        request,
        'reviewers/dashboard.html',
        context(
            **{
                # base_context includes motd.
                'sections': sections
            }
        ),
    )


@any_reviewer_required
def performance(request, user_id=False):
    user = request.user
    reviewers = _recent_reviewers()

    is_admin = acl.action_allowed(request, amo.permissions.REVIEWS_ADMIN)

    if is_admin and user_id:
        try:
            user = UserProfile.objects.get(pk=user_id)
        except UserProfile.DoesNotExist:
            pass  # Use request.user from above.

    monthly_data = _performance_by_month(user.id)
    performance_total = _performance_total(monthly_data)

    # Incentive point breakdown.
    today = date.today()
    month_ago = today - timedelta(days=30)
    year_ago = today - timedelta(days=365)
    point_total = ReviewerScore.get_total(user)
    totals = ReviewerScore.get_breakdown(user)
    months = ReviewerScore.get_breakdown_since(user, month_ago)
    years = ReviewerScore.get_breakdown_since(user, year_ago)

    def _sum(iter, types, exclude=False):
        """Sum the `total` property for items in `iter` that have an `atype`
        that is included in `types` when `exclude` is False (default) or not in
        `types` when `exclude` is True."""
        return sum(s.total for s in iter if (s.atype in types) == (not exclude))

    breakdown = {
        'month': {
            'addons': _sum(months, amo.GROUP_TYPE_ADDON),
            'themes': _sum(months, amo.GROUP_TYPE_THEME),
            'other': _sum(
                months, amo.GROUP_TYPE_ADDON + amo.GROUP_TYPE_THEME, exclude=True
            ),
        },
        'year': {
            'addons': _sum(years, amo.GROUP_TYPE_ADDON),
            'themes': _sum(years, amo.GROUP_TYPE_THEME),
            'other': _sum(
                years, amo.GROUP_TYPE_ADDON + amo.GROUP_TYPE_THEME, exclude=True
            ),
        },
        'total': {
            'addons': _sum(totals, amo.GROUP_TYPE_ADDON),
            'themes': _sum(totals, amo.GROUP_TYPE_THEME),
            'other': _sum(
                totals, amo.GROUP_TYPE_ADDON + amo.GROUP_TYPE_THEME, exclude=True
            ),
        },
    }

    data = context(
        monthly_data=json.dumps(monthly_data),
        performance_month=performance_total['month'],
        performance_year=performance_total['year'],
        breakdown=breakdown,
        point_total=point_total,
        reviewers=reviewers,
        current_user=user,
        is_admin=is_admin,
        is_user=(request.user.id == user.id),
    )

    return render(request, 'reviewers/performance.html', data)


def _recent_reviewers(days=90):
    since_date = datetime.now() - timedelta(days=days)
    reviewers = (
        UserProfile.objects.filter(
            activitylog__action__in=amo.LOG_REVIEWER_REVIEW_ACTION,
            activitylog__created__gt=since_date,
        )
        .exclude(id=settings.TASK_USER_ID)
        .order_by('display_name')
        .distinct()
    )
    return reviewers


def _performance_total(data):
    # TODO(gkoberger): Fix this so it's the past X, rather than this X to date.
    # (ex: March 15-April 15, not April 1 - April 15)
    total_yr = dict(usercount=0, teamamt=0, teamcount=0, teamavg=0)
    total_month = dict(usercount=0, teamamt=0, teamcount=0, teamavg=0)
    current_year = datetime.now().year

    for k, val in data.items():
        if k.startswith(str(current_year)):
            total_yr['usercount'] = total_yr['usercount'] + val['usercount']
            total_yr['teamamt'] = total_yr['teamamt'] + val['teamamt']
            total_yr['teamcount'] = total_yr['teamcount'] + val['teamcount']

    current_label_month = datetime.now().isoformat()[:7]
    if current_label_month in data:
        total_month = data[current_label_month]

    return dict(month=total_month, year=total_yr)


def _performance_by_month(user_id, months=12, end_month=None, end_year=None):
    monthly_data = OrderedDict()

    now = datetime.now()
    if not end_month:
        end_month = now.month
    if not end_year:
        end_year = now.year

    end_time = time.mktime((end_year, end_month + 1, 1, 0, 0, 0, 0, 0, -1))
    start_time = time.mktime((end_year, end_month + 1 - months, 1, 0, 0, 0, 0, 0, -1))

    sql = PerformanceGraph.objects.filter_raw(
        'log_activity.created >=', date.fromtimestamp(start_time).isoformat()
    ).filter_raw('log_activity.created <', date.fromtimestamp(end_time).isoformat())

    for row in sql.all():
        label = row.approval_created.isoformat()[:7]

        if label not in monthly_data:
            xaxis = row.approval_created.strftime('%b %Y')
            monthly_data[label] = dict(teamcount=0, usercount=0, teamamt=0, label=xaxis)

        monthly_data[label]['teamamt'] = monthly_data[label]['teamamt'] + 1
        monthly_data_count = monthly_data[label]['teamcount']
        monthly_data[label]['teamcount'] = monthly_data_count + row.total

        if row.user_id == user_id:
            user_count = monthly_data[label]['usercount']
            monthly_data[label]['usercount'] = user_count + row.total

    # Calculate averages
    for i, vals in monthly_data.items():
        average = round(vals['teamcount'] / float(vals['teamamt']), 1)
        monthly_data[i]['teamavg'] = str(average)  # floats aren't valid json

    return monthly_data


@permission_required(amo.permissions.ADDON_REVIEWER_MOTD_EDIT)
def motd(request):
    form = None
    form = MOTDForm(initial={'motd': get_config('reviewers_review_motd')})
    data = context(form=form)
    return render(request, 'reviewers/motd.html', data)


@permission_required(amo.permissions.ADDON_REVIEWER_MOTD_EDIT)
@post_required
def save_motd(request):
    form = MOTDForm(request.POST)
    if form.is_valid():
        set_config('reviewers_review_motd', form.cleaned_data['motd'])
        return redirect(reverse('reviewers.motd'))
    data = context(form=form)
    return render(request, 'reviewers/motd.html', data)


def is_admin_reviewer(request):
    return acl.action_allowed(request, amo.permissions.REVIEWS_ADMIN)


def filter_admin_review_for_legacy_queue(qs):
    return qs.filter(
        Q(needs_admin_code_review=None) | Q(needs_admin_code_review=False),
        Q(needs_admin_theme_review=None) | Q(needs_admin_theme_review=False),
    )


def _queue(request, TableObj, tab, unlisted=False, SearchForm=QueueSearchForm):
    admin_reviewer = is_admin_reviewer(request)
    qs = TableObj.get_queryset(admin_reviewer=admin_reviewer)

    if SearchForm:
        if request.GET:
            search_form = SearchForm(request.GET)
            if search_form.is_valid():
                qs = search_form.filter_qs(qs)
        else:
            search_form = SearchForm()
        is_searching = search_form.data.get('searching')
    else:
        search_form = None
        is_searching = False

    admin_reviewer = is_admin_reviewer(request)

    # Those restrictions will only work with our RawSQLModel, so we need to
    # make sure we're not dealing with a regular Django ORM queryset first.
    if hasattr(qs, 'sql_model'):
        if not is_searching and not admin_reviewer:
            qs = filter_admin_review_for_legacy_queue(qs)

    order_by = request.GET.get('sort', TableObj.default_order_by())
    if hasattr(TableObj, 'translate_sort_cols'):
        order_by = TableObj.translate_sort_cols(order_by)
    table = TableObj(data=qs, order_by=order_by)
    per_page = request.GET.get('per_page', REVIEWS_PER_PAGE)
    try:
        per_page = int(per_page)
    except ValueError:
        per_page = REVIEWS_PER_PAGE
    if per_page <= 0 or per_page > REVIEWS_PER_PAGE_MAX:
        per_page = REVIEWS_PER_PAGE
    page = paginate(request, table.rows, per_page=per_page, count=qs.count())
    table.set_page(page)

    return render(
        request,
        'reviewers/queue.html',
        context(
            table=table,
            page=page,
            tab=tab,
            search_form=search_form,
            point_types=amo.REVIEWED_AMO,
            unlisted=unlisted,
        ),
    )


def fetch_queue_counts(admin_reviewer):
    def construct_count_queryset_from_sql_model(sqlmodel):
        # FIXME: ideally here we'd prevent sqlmodel.objects from including
        # all the columns in the query for the count like it's done below with
        # actual querysets...
        qs = sqlmodel.objects

        if not admin_reviewer:
            qs = filter_admin_review_for_legacy_queue(qs)
        return qs.count

    def construct_count_queryset_from_queryset(qs):
        # Our querysets can have distinct, which causes django to run the full
        # select in a subquery and then count() on it. That's tracked in
        # https://code.djangoproject.com/ticket/30685
        # We can't easily fix the fact that there is a subquery, but we can
        # avoid selecting all fields and ordering needlessly.
        return qs.values('pk').order_by().count

    counts = {
        'extension': construct_count_queryset_from_sql_model(ViewExtensionQueue),
        'theme_pending': construct_count_queryset_from_sql_model(ViewThemePendingQueue),
        'theme_nominated': construct_count_queryset_from_sql_model(
            ViewThemeFullReviewQueue
        ),
        'recommended': construct_count_queryset_from_sql_model(ViewRecommendedQueue),
        'moderated': construct_count_queryset_from_queryset(
            Rating.objects.all().to_moderate()
        ),
        'auto_approved': construct_count_queryset_from_queryset(
            Addon.objects.get_auto_approved_queue(admin_reviewer=admin_reviewer)
        ),
        'content_review': construct_count_queryset_from_queryset(
            Addon.objects.get_content_review_queue(admin_reviewer=admin_reviewer)
        ),
        'pending_rejection': construct_count_queryset_from_queryset(
            Addon.objects.get_pending_rejection_queue(admin_reviewer=admin_reviewer)
        ),
    }
    return {queue: count() for (queue, count) in counts.items()}


@permission_or_tools_view_required(amo.permissions.ADDONS_REVIEW)
def queue_extension(request):
    return _queue(request, view_table_factory(ViewExtensionQueue), 'extension')


@permission_or_tools_view_required(amo.permissions.ADDONS_RECOMMENDED_REVIEW)
def queue_recommended(request):
    return _queue(request, view_table_factory(ViewRecommendedQueue), 'recommended')


@permission_or_tools_view_required(amo.permissions.STATIC_THEMES_REVIEW)
def queue_theme_nominated(request):
    return _queue(
        request, view_table_factory(ViewThemeFullReviewQueue), 'theme_nominated'
    )


@permission_or_tools_view_required(amo.permissions.STATIC_THEMES_REVIEW)
def queue_theme_pending(request):
    return _queue(request, view_table_factory(ViewThemePendingQueue), 'theme_pending')


@permission_or_tools_view_required(amo.permissions.RATINGS_MODERATE)
def queue_moderated(request):
    qs = Rating.objects.all().to_moderate().order_by('ratingflag__created')
    page = paginate(request, qs, per_page=20)

    flags = dict(RatingFlag.FLAGS)

    reviews_formset = RatingFlagFormSet(
        request.POST or None, queryset=page.object_list, request=request
    )

    if request.method == 'POST':
        if reviews_formset.is_valid():
            reviews_formset.save()
        else:
            amo.messages.error(
                request,
                ' '.join(
                    e.as_text() or ugettext('An unknown error occurred')
                    for e in reviews_formset.errors
                ),
            )
        return redirect(reverse('reviewers.queue_moderated'))

    return render(
        request,
        'reviewers/queue.html',
        context(
            reviews_formset=reviews_formset,
            tab='moderated',
            page=page,
            flags=flags,
            search_form=None,
            point_types=amo.REVIEWED_AMO,
        ),
    )


@any_reviewer_required
@json_view
def application_versions_json(request):
    app_id = request.GET.get('application_id', amo.FIREFOX.id)
    form = QueueSearchForm()
    return {'choices': form.version_choices_for_app_id(app_id)}


@permission_or_tools_view_required(amo.permissions.ADDONS_CONTENT_REVIEW)
def queue_content_review(request):
    return _queue(request, ContentReviewTable, 'content_review', SearchForm=None)


@permission_or_tools_view_required(amo.permissions.ADDONS_REVIEW)
def queue_auto_approved(request):
    return _queue(request, AutoApprovedTable, 'auto_approved', SearchForm=None)


@permission_or_tools_view_required(amo.permissions.ADDONS_REVIEW)
def queue_scanners(request):
    return _queue(request, ScannersReviewTable, 'scanners', SearchForm=None)


@permission_or_tools_view_required(amo.permissions.ADDONS_REVIEW)
def queue_mad(request):
    return _queue(request, MadReviewTable, 'mad', SearchForm=None)


@permission_or_tools_view_required(amo.permissions.REVIEWS_ADMIN)
def queue_pending_rejection(request):
    return _queue(request, PendingRejectionTable, 'pending_rejection', SearchForm=None)


def determine_channel(channel_as_text):
    """Determine which channel the review is for according to the channel
    parameter as text, and whether we should be in content-review only mode."""
    if channel_as_text == 'content':
        # 'content' is not a real channel, just a different review mode for
        # listed add-ons.
        content_review = True
        channel = 'listed'
    else:
        content_review = False
    # channel is passed in as text, but we want the constant.
    channel = amo.CHANNEL_CHOICES_LOOKUP.get(
        channel_as_text, amo.RELEASE_CHANNEL_LISTED
    )
    return channel, content_review


@login_required
@any_reviewer_required  # Additional permission checks are done inside.
@reviewer_addon_view_factory
def review(request, addon, channel=None):
    whiteboard_url = reverse(
        'reviewers.whiteboard',
        args=(channel or 'listed', addon.slug if addon.slug else addon.pk),
    )
    channel, content_review = determine_channel(channel)

    was_auto_approved = (
        channel == amo.RELEASE_CHANNEL_LISTED
        and addon.current_version
        and addon.current_version.was_auto_approved
    )
    is_static_theme = addon.type == amo.ADDON_STATICTHEME
    promoted_group = addon.promoted_group(currently_approved=False)

    # Are we looking at an unlisted review page, or (weirdly) the listed
    # review page of an unlisted-only add-on?
    unlisted_only = (
        channel == amo.RELEASE_CHANNEL_UNLISTED
        or not addon.has_listed_versions(include_deleted=True)
    )
    if unlisted_only and not acl.check_unlisted_addons_reviewer(request):
        raise PermissionDenied

    # Are we looking at a listed review page while only having content review
    # permissions ? Redirect to content review page, it will be more useful.
    if (
        channel == amo.RELEASE_CHANNEL_LISTED
        and content_review is False
        and acl.action_allowed(request, amo.permissions.ADDONS_CONTENT_REVIEW)
        and not acl.is_reviewer(request, addon, allow_content_reviewers=False)
    ):
        return redirect('reviewers.review', 'content', addon.pk)

    # Other cases are handled in ReviewHelper by limiting what actions are
    # available depending on user permissions and add-on/version state.

    version = addon.find_latest_version(channel=channel, exclude=())
    latest_not_disabled_version = addon.find_latest_version(channel=channel)

    if not settings.ALLOW_SELF_REVIEWS and addon.has_author(request.user):
        amo.messages.warning(request, ugettext('Self-reviews are not allowed.'))
        return redirect(reverse('reviewers.dashboard'))

    # Queryset to be paginated for versions. We use the default ordering to get
    # most recently created first (Note that the template displays each page
    # in reverse order, older first).
    versions_qs = (
        # We want to load all Versions, even deleted ones, while using the
        # addon.versions related manager to get `addon` property pre-cached on
        # each version.
        addon.versions(manager='unfiltered_for_relations')
        .filter(channel=channel)
        .select_related('autoapprovalsummary')
        .select_related('reviewerflags')
        # Prefetch scanner results... but without the results json as we don't
        # need it.
        .prefetch_related(
            Prefetch(
                'scannerresults',
                queryset=ScannerResult.objects.defer('results'),
            )
        )
        # Add activity transformer to prefetch all related activity logs on
        # top of the regular transformers.
        .transform(Version.transformer_activity)
        # Add auto_approvable transformer to prefetch information about whether
        # each version is auto-approvable or not.
        .transform(Version.transformer_auto_approvable)
    )

    form_helper = ReviewHelper(
        request=request, addon=addon, version=version, content_review=content_review
    )
    form = ReviewForm(
        request.POST if request.method == 'POST' else None, helper=form_helper
    )
    is_admin = acl.action_allowed(request, amo.permissions.REVIEWS_ADMIN)

    approvals_info = None
    reports = Paginator(
        (
            AbuseReport.objects.filter(
                Q(addon=addon) | Q(user__in=addon.listed_authors)
            )
            .select_related('user')
            .prefetch_related(
                # Should only need translations for addons on abuse reports,
                # so let's prefetch the add-on with them and avoid repeating
                # a ton of potentially duplicate queries with all the useless
                # Addon transforms.
                Prefetch('addon', queryset=Addon.unfiltered.all().only_translations())
            )
            .order_by('-created')
        ),
        5,
    ).page(1)
    user_ratings = Paginator(
        (
            Rating.without_replies.filter(
                addon=addon, rating__lte=3, body__isnull=False
            ).order_by('-created')
        ),
        5,
    ).page(1)
    if channel == amo.RELEASE_CHANNEL_LISTED:
        if was_auto_approved:
            try:
                approvals_info = addon.addonapprovalscounter
            except AddonApprovalsCounter.DoesNotExist:
                pass

        if content_review:
            queue_type = 'content_review'
        elif promoted_group == RECOMMENDED:
            queue_type = 'recommended'
        elif was_auto_approved:
            queue_type = 'auto_approved'
        elif is_static_theme:
            queue_type = form.helper.handler.review_type
        else:
            queue_type = 'extension'
        redirect_url = reverse('reviewers.queue_%s' % queue_type)
    else:
        redirect_url = reverse('reviewers.review', args=['unlisted', addon.pk])

    if request.method == 'POST' and form.is_valid():
        # Execute the action (is_valid() ensures the action is available to the
        # reviewer)
        form.helper.process()

        amo.messages.success(request, ugettext('Review successfully processed.'))
        clear_reviewing_cache(addon.id)
        return redirect(form.helper.redirect_url or redirect_url)

    # Kick off validation tasks for any files in this version which don't have
    # cached validation, since reviewers will almost certainly need to access
    # them. But only if we're not running in eager mode, since that could mean
    # blocking page load for several minutes.
    if version and not getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
        for file_ in version.all_files:
            if not file_.has_been_validated:
                devhub_tasks.validate(file_)

    actions = form.helper.actions.items()

    try:
        # Find the previously approved version to compare to.
        base_version = version and (
            addon.versions.exclude(id=version.id)
            .filter(
                # We're looking for a version that was either manually approved
                # (either it has no auto approval summary, or it has one but
                # with a negative verdict because it was locked by a reviewer
                # who then approved it themselves), or auto-approved but then
                # confirmed.
                Q(autoapprovalsummary__isnull=True)
                | Q(autoapprovalsummary__verdict=amo.NOT_AUTO_APPROVED)
                | Q(
                    autoapprovalsummary__verdict=amo.AUTO_APPROVED,
                    autoapprovalsummary__confirmed=True,
                )
            )
            .filter(
                channel=channel,
                files__isnull=False,
                created__lt=version.created,
                files__status=amo.STATUS_APPROVED,
            )
            .latest()
        )
    except Version.DoesNotExist:
        base_version = None

    # The actions we shouldn't show a minimal form for.
    actions_full = []
    # The actions we should show the comments form for (contrary to minimal
    # form above, it defaults to True, because most actions do need to have
    # the comments form).
    actions_comments = []
    # The actions for which we should display the delayed rejection fields.
    actions_delayable = []

    for key, action in actions:
        if not (is_static_theme or action.get('minimal')):
            actions_full.append(key)
        if action.get('comments', True):
            actions_comments.append(key)
        if action.get('delayable', False):
            actions_delayable.append(key)

    deleted_addon_ids = (
        AddonGUID.objects.filter(guid=addon.guid)
        .exclude(addon=addon)
        .values_list('addon_id', flat=True)
        if addon.guid
        else []
    )

    pager = paginate(request, versions_qs, 10)
    num_pages = pager.paginator.num_pages
    count = pager.paginator.count

    auto_approval_info = {}
    version_ids = []
    # Now that we've paginated the versions queryset, iterate on them to
    # generate auto approvals info. Note that the variable should not clash
    # the already existing 'version'.
    for a_version in pager.object_list:
        version_ids.append(a_version.pk)
        if not a_version.is_ready_for_auto_approval:
            continue
        try:
            summary = a_version.autoapprovalsummary
        except AutoApprovalSummary.DoesNotExist:
            auto_approval_info[a_version.pk] = None
            continue
        # Call calculate_verdict() again, it will use the data already stored.
        verdict_info = summary.calculate_verdict(pretty=True)
        auto_approval_info[a_version.pk] = verdict_info

    versions_pending_rejection_qs = versions_qs.filter(
        reviewerflags__pending_rejection__isnull=False
    )
    has_versions_pending_rejection = versions_pending_rejection_qs.exists()
    # We want to notify the reviewer if there are versions needing extra
    # attention that are not present in the versions history (which is
    # paginated).
    versions_flagged_by_scanners_other = (
        versions_qs.filter(needs_human_review=True).exclude(pk__in=version_ids).count()
    )
    versions_flagged_for_human_review_other = (
        versions_qs.filter(reviewerflags__needs_human_review_by_mad=True)
        .exclude(pk__in=version_ids)
        .count()
    )
    versions_pending_rejection_other = versions_pending_rejection_qs.exclude(
        pk__in=version_ids
    ).count()

    flags = get_flags(addon, version) if version else []

    try:
        whiteboard = Whiteboard.objects.get(pk=addon.pk)
    except Whiteboard.DoesNotExist:
        whiteboard = Whiteboard(pk=addon.pk)

    wb_form_cls = PublicWhiteboardForm if is_static_theme else WhiteboardForm
    whiteboard_form = wb_form_cls(instance=whiteboard, prefix='whiteboard')

    user_changes_actions = [
        amo.LOG.ADD_USER_WITH_ROLE.id,
        amo.LOG.CHANGE_USER_WITH_ROLE.id,
        amo.LOG.REMOVE_USER_WITH_ROLE.id,
    ]
    user_changes_log = ActivityLog.objects.filter(
        action__in=user_changes_actions, addonlog__addon=addon
    ).order_by('id')

    name_translations = (
        addon.name.__class__.objects.filter(
            id=addon.name.id, localized_string__isnull=False
        ).exclude(localized_string='')
        if addon.name
        else []
    )

    ctx = context(
        # Used for reviewer subscription check, don't use global `is_reviewer`
        # since that actually is `is_user_any_kind_of_reviewer`.
        acl_is_reviewer=acl.is_reviewer(request, addon),
        acl_is_review_moderator=(
            acl.action_allowed(request, amo.permissions.RATINGS_MODERATE)
            and request.user.is_staff
        ),
        actions=actions,
        actions_comments=actions_comments,
        actions_delayable=actions_delayable,
        actions_full=actions_full,
        addon=addon,
        api_token=request.COOKIES.get(API_TOKEN_COOKIE, None),
        approvals_info=approvals_info,
        auto_approval_info=auto_approval_info,
        base_version=base_version,
        content_review=content_review,
        count=count,
        deleted_addon_ids=deleted_addon_ids,
        flags=flags,
        form=form,
        has_versions_pending_rejection=has_versions_pending_rejection,
        is_admin=is_admin,
        latest_not_disabled_version=latest_not_disabled_version,
        latest_version_is_unreviewed_and_not_pending_rejection=(
            version
            and version.channel == amo.RELEASE_CHANNEL_LISTED
            and version.is_unreviewed
            and not version.pending_rejection
        ),
        promoted_group=promoted_group,
        name_translations=name_translations,
        now=datetime.now(),
        num_pages=num_pages,
        pager=pager,
        reports=reports,
        subscribed_listed=ReviewerSubscription.objects.filter(
            user=request.user, addon=addon, channel=amo.RELEASE_CHANNEL_LISTED
        ).exists(),
        subscribed_unlisted=ReviewerSubscription.objects.filter(
            user=request.user, addon=addon, channel=amo.RELEASE_CHANNEL_UNLISTED
        ).exists(),
        unlisted=(channel == amo.RELEASE_CHANNEL_UNLISTED),
        user_changes_log=user_changes_log,
        user_ratings=user_ratings,
        version=version,
        versions_flagged_by_scanners_other=versions_flagged_by_scanners_other,
        versions_flagged_for_human_review_other=versions_flagged_for_human_review_other,  # noqa
        versions_pending_rejection_other=versions_pending_rejection_other,
        whiteboard_form=whiteboard_form,
        whiteboard_url=whiteboard_url,
    )
    return render(request, 'reviewers/review.html', ctx)


@never_cache
@json_view
# This will 403 for users with only ReviewerTools:View, but they shouldn't
# acquire reviewer locks anyway, and it's not a big deal if they don't see
# existing locks.
@any_reviewer_required
def review_viewing(request):
    if 'addon_id' not in request.POST:
        return {}

    addon_id = request.POST['addon_id']
    user_id = request.user.id
    current_name = ''
    is_user = 0
    key = get_reviewing_cache_key(addon_id)
    user_key = 'review_viewing_user:{user_id}'.format(user_id=user_id)
    interval = amo.REVIEWER_VIEWING_INTERVAL

    # Check who is viewing.
    currently_viewing = get_reviewing_cache(addon_id)

    # If nobody is viewing or current user is, set current user as viewing
    if not currently_viewing or currently_viewing == user_id:
        # Get a list of all the reviews this user is locked on.
        review_locks = cache.get_many(cache.get(user_key, {}))
        can_lock_more_reviews = len(
            review_locks
        ) < amo.REVIEWER_REVIEW_LOCK_LIMIT or acl.action_allowed(
            request, amo.permissions.REVIEWS_ADMIN
        )
        if can_lock_more_reviews or currently_viewing == user_id:
            set_reviewing_cache(addon_id, user_id)
            # Give it double expiry just to be safe.
            cache.set(user_key, set(review_locks) | {key}, interval * 4)
            currently_viewing = user_id
            current_name = request.user.name
            is_user = 1
        else:
            currently_viewing = settings.TASK_USER_ID
            current_name = ugettext('Review lock limit reached')
            is_user = 2
    else:
        current_name = UserProfile.objects.get(pk=currently_viewing).name

    return {
        'current': currently_viewing,
        'current_name': current_name,
        'is_user': is_user,
        'interval_seconds': interval,
    }


@never_cache
@json_view
@any_reviewer_required
def queue_viewing(request):
    addon_ids = request.GET.get('addon_ids')
    if not addon_ids:
        return {}

    viewing = {}
    user_id = request.user.id

    for addon_id in addon_ids.split(','):
        addon_id = addon_id.strip()
        key = get_reviewing_cache_key(addon_id)
        currently_viewing = cache.get(key)
        if currently_viewing and currently_viewing != user_id:
            viewing[addon_id] = UserProfile.objects.get(id=currently_viewing).name

    return viewing


@json_view
@any_reviewer_required
def queue_version_notes(request, addon_id):
    addon = get_object_or_404(Addon.objects, pk=addon_id)
    version = addon.latest_version
    return {
        'release_notes': str(version.release_notes),
        'approval_notes': version.approval_notes,
    }


@json_view
@any_reviewer_required
def queue_review_text(request, log_id):
    review = get_object_or_404(CommentLog, activity_log_id=log_id)
    return {'reviewtext': review.comments}


@any_reviewer_required
def reviewlog(request):
    data = request.GET.copy()

    if not data.get('start') and not data.get('end'):
        today = date.today()
        data['start'] = date(today.year, today.month, 1)

    form = ReviewLogForm(data)

    approvals = ActivityLog.objects.review_log()
    if not acl.check_unlisted_addons_reviewer(request):
        # Only display logs related to unlisted versions to users with the
        # right permission.
        list_channel = amo.RELEASE_CHANNEL_LISTED
        approvals = approvals.filter(versionlog__version__channel=list_channel)
    if not acl.check_addons_reviewer(request):
        approvals = approvals.exclude(
            versionlog__version__addon__type__in=amo.GROUP_TYPE_ADDON
        )
    if not acl.check_static_theme_reviewer(request):
        approvals = approvals.exclude(
            versionlog__version__addon__type=amo.ADDON_STATICTHEME
        )

    if form.is_valid():
        data = form.cleaned_data
        if data['start']:
            approvals = approvals.filter(created__gte=data['start'])
        if data['end']:
            approvals = approvals.filter(created__lt=data['end'])
        if data['search']:
            term = data['search']
            approvals = approvals.filter(
                Q(commentlog__comments__icontains=term)
                | Q(addonlog__addon__name__localized_string__icontains=term)
                | Q(user__display_name__icontains=term)
                | Q(user__username__icontains=term)
            ).distinct()

    pager = amo.utils.paginate(request, approvals, 50)
    data = context(form=form, pager=pager)
    return render(request, 'reviewers/reviewlog.html', data)


@any_reviewer_required
@reviewer_addon_view_factory
def abuse_reports(request, addon):
    developers = addon.listed_authors
    reports = (
        AbuseReport.objects.filter(Q(addon=addon) | Q(user__in=developers))
        .select_related('user')
        .prefetch_related(
            # See review(): we only need the add-on objects and their translations.
            Prefetch('addon', queryset=Addon.unfiltered.all().only_translations()),
        )
        .order_by('-created')
    )
    reports = amo.utils.paginate(request, reports)
    data = context(addon=addon, reports=reports, version=addon.current_version)
    return render(request, 'reviewers/abuse_reports.html', data)


@any_reviewer_required
def leaderboard(request):
    return render(
        request,
        'reviewers/leaderboard.html',
        context(scores=ReviewerScore.all_users_by_score()),
    )


@any_reviewer_required
@reviewer_addon_view_factory
def whiteboard(request, addon, channel):
    channel_as_text = channel
    channel, content_review = determine_channel(channel)

    unlisted_only = (
        channel == amo.RELEASE_CHANNEL_UNLISTED
        or not addon.has_listed_versions(include_deleted=True)
    )
    if unlisted_only and not acl.check_unlisted_addons_reviewer(request):
        raise PermissionDenied

    whiteboard, _ = Whiteboard.objects.get_or_create(pk=addon.pk)
    form = WhiteboardForm(
        request.POST or None, instance=whiteboard, prefix='whiteboard'
    )

    if form.is_valid():
        if whiteboard.private or whiteboard.public:
            form.save()
        else:
            whiteboard.delete()

        return redirect(
            'reviewers.review', channel_as_text, addon.slug if addon.slug else addon.pk
        )
    raise PermissionDenied


@unlisted_addons_reviewer_required
def unlisted_list(request):
    return _queue(
        request,
        ViewUnlistedAllListTable,
        'all',
        unlisted=True,
        SearchForm=AllAddonSearchForm,
    )


def policy_viewer(request, addon, eula_or_privacy, page_title, long_title):
    if not eula_or_privacy:
        raise http.Http404
    channel_text = request.GET.get('channel')
    channel, content_review = determine_channel(channel_text)

    review_url = reverse(
        'reviewers.review',
        args=(channel_text or 'listed', addon.slug if addon.slug else addon.pk),
    )
    return render(
        request,
        'reviewers/policy_view.html',
        {
            'addon': addon,
            'review_url': review_url,
            'content': eula_or_privacy,
            'page_title': page_title,
            'long_title': long_title,
        },
    )


@any_reviewer_required
@reviewer_addon_view_factory
def eula(request, addon):
    return policy_viewer(
        request,
        addon,
        addon.eula,
        page_title=ugettext('{addon} – EULA'),
        long_title=ugettext('End-User License Agreement'),
    )


@any_reviewer_required
@reviewer_addon_view_factory
def privacy(request, addon):
    return policy_viewer(
        request,
        addon,
        addon.privacy_policy,
        page_title=ugettext('{addon} – Privacy Policy'),
        long_title=ugettext('Privacy Policy'),
    )


@any_reviewer_required
@json_view
def theme_background_images(request, version_id):
    """similar to devhub.views.theme_background_image but returns all images"""
    version = get_object_or_404(Version, id=int(version_id))
    return version.get_background_images_encoded(header_only=False)


@login_required
@set_csp(**settings.RESTRICTED_DOWNLOAD_CSP)
def download_git_stored_file(request, version_id, filename):
    version = get_object_or_404(Version.unfiltered, id=int(version_id))

    try:
        addon = version.addon
    except Addon.DoesNotExist:
        raise http.Http404

    if version.channel == amo.RELEASE_CHANNEL_LISTED:
        is_owner = acl.check_addon_ownership(request, addon, dev=True)
        if not (acl.is_reviewer(request, addon) or is_owner):
            raise PermissionDenied
    else:
        if not owner_or_unlisted_reviewer(request, addon):
            raise http.Http404

    file = version.current_file

    serializer = FileInfoSerializer(
        instance=file,
        context={'file': filename, 'request': request, 'version': version},
    )

    tree = serializer.tree

    try:
        blob_or_tree = tree[serializer._get_selected_file()]

        if blob_or_tree.type == pygit2.GIT_OBJ_TREE:
            return http.HttpResponseBadRequest("Can't serve directories")
        selected_file = serializer._get_entries()[filename]
    except (KeyError, NotFound):
        raise http.Http404()

    actual_blob = serializer.git_repo[blob_or_tree.oid]

    response = http.HttpResponse(
        content=actual_blob.data, content_type=serializer.get_mimetype(file)
    )

    # Backported from Django 2.1 to handle unicode filenames properly
    selected_filename = selected_file['filename']
    try:
        selected_filename.encode('ascii')
        file_expr = 'filename="{}"'.format(selected_filename)
    except UnicodeEncodeError:
        file_expr = "filename*=utf-8''{}".format(urlquote(selected_filename))

    response['Content-Disposition'] = 'attachment; {}'.format(file_expr)
    response['Content-Length'] = actual_blob.size

    return response


class AddonReviewerViewSet(GenericViewSet):
    log = olympia.core.logger.getLogger('z.reviewers')

    @drf_action(
        detail=True, methods=['post'], permission_classes=[AllowAnyKindOfReviewer]
    )
    def subscribe(self, request, **kwargs):
        return self.subscribe_listed(request, **kwargs)

    @drf_action(
        detail=True, methods=['post'], permission_classes=[AllowAnyKindOfReviewer]
    )
    def subscribe_listed(self, request, **kwargs):
        addon = get_object_or_404(Addon, pk=kwargs['pk'])
        ReviewerSubscription.objects.get_or_create(
            user=request.user, addon=addon, channel=amo.RELEASE_CHANNEL_LISTED
        )
        return Response(status=status.HTTP_202_ACCEPTED)

    @drf_action(
        detail=True, methods=['post'], permission_classes=[AllowAnyKindOfReviewer]
    )
    def unsubscribe(self, request, **kwargs):
        return self.unsubscribe_listed(request, **kwargs)

    @drf_action(
        detail=True, methods=['post'], permission_classes=[AllowAnyKindOfReviewer]
    )
    def unsubscribe_listed(self, request, **kwargs):
        addon = get_object_or_404(Addon, pk=kwargs['pk'])
        ReviewerSubscription.objects.filter(
            user=request.user, addon=addon, channel=amo.RELEASE_CHANNEL_LISTED
        ).delete()
        return Response(status=status.HTTP_202_ACCEPTED)

    @drf_action(
        detail=True, methods=['post'], permission_classes=[AllowReviewerUnlisted]
    )
    def subscribe_unlisted(self, request, **kwargs):
        addon = get_object_or_404(Addon, pk=kwargs['pk'])
        ReviewerSubscription.objects.get_or_create(
            user=request.user, addon=addon, channel=amo.RELEASE_CHANNEL_UNLISTED
        )
        return Response(status=status.HTTP_202_ACCEPTED)

    @drf_action(
        detail=True, methods=['post'], permission_classes=[AllowReviewerUnlisted]
    )
    def unsubscribe_unlisted(self, request, **kwargs):
        addon = get_object_or_404(Addon, pk=kwargs['pk'])
        ReviewerSubscription.objects.filter(
            user=request.user, addon=addon, channel=amo.RELEASE_CHANNEL_UNLISTED
        ).delete()
        return Response(status=status.HTTP_202_ACCEPTED)

    @drf_action(
        detail=True,
        methods=['post'],
        permission_classes=[GroupPermission(amo.permissions.REVIEWS_ADMIN)],
    )
    def disable(self, request, **kwargs):
        addon = get_object_or_404(Addon, pk=kwargs['pk'])
        addon.force_disable()
        return Response(status=status.HTTP_202_ACCEPTED)

    @drf_action(
        detail=True,
        methods=['post'],
        permission_classes=[GroupPermission(amo.permissions.REVIEWS_ADMIN)],
    )
    def enable(self, request, **kwargs):
        addon = get_object_or_404(Addon, pk=kwargs['pk'])
        addon.force_enable()
        return Response(status=status.HTTP_202_ACCEPTED)

    @drf_action(
        detail=True,
        methods=['patch'],
        permission_classes=[GroupPermission(amo.permissions.REVIEWS_ADMIN)],
    )
    def flags(self, request, **kwargs):
        addon = get_object_or_404(Addon, pk=kwargs['pk'])
        instance, _ = AddonReviewerFlags.objects.get_or_create(addon=addon)
        serializer = AddonReviewerFlagsSerializer(
            instance, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    @drf_action(
        detail=True,
        methods=['post'],
        permission_classes=[GroupPermission(amo.permissions.REVIEWS_ADMIN)],
    )
    def deny_resubmission(self, request, **kwargs):
        addon = get_object_or_404(Addon, pk=kwargs['pk'])
        status_code = status.HTTP_202_ACCEPTED
        try:
            addon.deny_resubmission()
        except RuntimeError:
            status_code = status.HTTP_409_CONFLICT
        return Response(status=status_code)

    @drf_action(
        detail=True,
        methods=['post'],
        permission_classes=[GroupPermission(amo.permissions.REVIEWS_ADMIN)],
    )
    def allow_resubmission(self, request, **kwargs):
        addon = get_object_or_404(Addon, pk=kwargs['pk'])
        status_code = status.HTTP_202_ACCEPTED
        try:
            addon.allow_resubmission()
        except RuntimeError:
            status_code = status.HTTP_409_CONFLICT
        return Response(status=status_code)

    @drf_action(
        detail=True,
        methods=['post'],
        permission_classes=[GroupPermission(amo.permissions.REVIEWS_ADMIN)],
    )
    def clear_pending_rejections(self, request, **kwargs):
        addon = get_object_or_404(Addon, pk=kwargs['pk'])
        status_code = status.HTTP_202_ACCEPTED
        VersionReviewerFlags.objects.filter(version__addon=addon).update(
            pending_rejection=None
        )
        return Response(status=status_code)

    @drf_action(
        detail=True,
        methods=['get'],
        permission_classes=[AllowAnyKindOfReviewer],
        url_path=r'file/(?P<file_id>[^/]+)/validation',
    )
    def json_file_validation(self, request, **kwargs):
        addon = get_object_or_404(Addon.unfiltered.id_or_slug(kwargs['pk']))
        file = get_object_or_404(File, version__addon=addon, id=kwargs['file_id'])
        if file.version.channel == amo.RELEASE_CHANNEL_UNLISTED:
            if not acl.check_unlisted_addons_reviewer(request):
                raise PermissionDenied
        elif not acl.is_reviewer(request, addon):
            raise PermissionDenied
        try:
            result = file.validation
        except File.validation.RelatedObjectDoesNotExist:
            raise http.Http404
        return JsonResponse(
            {
                'validation': result.processed_validation,
            }
        )


class ReviewAddonVersionMixin(object):
    permission_classes = [AnyOf(AllowReviewer, AllowReviewerUnlisted)]

    def get_queryset(self):
        # Permission classes disallow access to non-public/unlisted add-ons
        # unless logged in as a reviewer/addon owner/admin, so we don't have to
        # filter the base queryset here.
        addon = self.get_addon_object()

        qs = (
            addon.versions(manager='unfiltered_for_relations')
            .all()
            # We don't need any transforms on the version, not even
            # translations.
            .no_transforms()
            .order_by('-created')
        )

        if not self.can_access_unlisted():
            qs = qs.filter(channel=amo.RELEASE_CHANNEL_LISTED)

        return qs

    def can_access_unlisted(self):
        """Return True if we can access unlisted versions with the current
        request. Cached on the viewset instance."""
        if not hasattr(self, 'can_view_unlisted'):
            # Allow viewing unlisted for reviewers with permissions or
            # addon authors.
            addon = self.get_addon_object()
            self.can_view_unlisted = acl.check_unlisted_addons_reviewer(
                self.request
            ) or addon.has_author(self.request.user)
        return self.can_view_unlisted

    def get_addon_object(self):
        if not hasattr(self, 'addon_object'):
            # We only need translations on the add-on, no other transforms.
            self.addon_object = get_object_or_404(
                Addon.unfiltered.all().only_translations(),
                pk=self.kwargs.get('addon_pk'),
            )
        return self.addon_object

    def check_permissions(self, request):
        if self.action == 'list':
            # When listing DRF doesn't explicitly check for object permissions
            # but here we need to do that against the parent add-on.
            # So we're calling check_object_permission() ourselves,
            # which will pass down the addon object directly.
            return super(ReviewAddonVersionMixin, self).check_object_permissions(
                request, self.get_addon_object()
            )

        super(ReviewAddonVersionMixin, self).check_permissions(request)

    def check_object_permissions(self, request, obj):
        """
        Check if the request should be permitted for a given version object.
        Raises an appropriate exception if the request is not permitted.
        """
        # If the instance is marked as deleted and the client is not allowed to
        # see deleted instances, we want to return a 404, behaving as if it
        # does not exist.
        if obj.deleted and not (
            GroupPermission(amo.permissions.ADDONS_VIEW_DELETED).has_object_permission(
                self.request, self, obj.addon
            )
        ):
            raise http.Http404

        # Now check permissions using DRF implementation on the add-on, it
        # should be all we need.
        return super().check_object_permissions(request, obj.addon)


class ReviewAddonVersionViewSet(
    ReviewAddonVersionMixin, ListModelMixin, RetrieveModelMixin, GenericViewSet
):
    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['file'] = self.request.GET.get('file', None)

        if self.request.GET.get('file_only', 'false') == 'true':
            context['exclude_entries'] = True

        return context

    def get_serializer_class(self):
        if self.request.GET.get('file_only', 'false') == 'true':
            return AddonBrowseVersionSerializerFileOnly
        return AddonBrowseVersionSerializer

    def list(self, request, *args, **kwargs):
        """Return all (re)viewable versions for this add-on.

        Full list, no pagination."""
        qs = self.filter_queryset(self.get_queryset())
        serializer = DiffableVersionSerializer(qs, many=True)
        return Response(serializer.data)


class ReviewAddonVersionDraftCommentViewSet(
    RetrieveModelMixin,
    ListModelMixin,
    CreateModelMixin,
    DestroyModelMixin,
    UpdateModelMixin,
    GenericViewSet,
):

    permission_classes = [AnyOf(AllowReviewer, AllowReviewerUnlisted)]

    queryset = DraftComment.objects.all()
    serializer_class = DraftCommentSerializer

    def check_object_permissions(self, request, obj):
        """Check permissions against the parent add-on object."""
        return super().check_object_permissions(request, obj.version.addon)

    def _verify_object_permissions(self, object_to_verify, version):
        """Verify permissions.

        This method works for `Version` and `DraftComment` objects.
        """
        # If the instance is marked as deleted and the client is not allowed to
        # see deleted instances, we want to return a 404, behaving as if it
        # does not exist.
        if version.deleted and not (
            GroupPermission(amo.permissions.ADDONS_VIEW_DELETED).has_object_permission(
                self.request, self, version.addon
            )
        ):
            raise http.Http404

        # Now we can checking permissions
        super().check_object_permissions(self.request, version.addon)

    def get_queryset(self):
        # Preload version once for all drafts returned, and join with user and
        # canned response to avoid extra queries for those.
        return (
            self.get_version_object()
            .draftcomment_set.all()
            .select_related('user', 'canned_response')
        )

    def get_object(self, **kwargs):
        qset = self.filter_queryset(self.get_queryset())

        kwargs.setdefault(
            self.lookup_field,
            self.kwargs.get(self.lookup_url_kwarg or self.lookup_field),
        )

        obj = get_object_or_404(qset, **kwargs)
        self._verify_object_permissions(obj, obj.version)
        return obj

    def get_addon_object(self):
        if not hasattr(self, 'addon_object'):
            self.addon_object = get_object_or_404(
                # The serializer will not need to return much info about the
                # addon, so we can use just the translations transformer and
                # avoid the rest.
                Addon.unfiltered.all().only_translations(),
                pk=self.kwargs['addon_pk'],
            )
        return self.addon_object

    def get_version_object(self):
        if not hasattr(self, 'version_object'):
            self.version_object = get_object_or_404(
                # The serializer will not need any of the stuff the
                # transformers give us for the version. We do need to fetch
                # using an unfiltered manager to see deleted versions, though.
                self.get_addon_object()
                .versions(manager='unfiltered_for_relations')
                .all()
                .no_transforms(),
                pk=self.kwargs['version_pk'],
            )
            self._verify_object_permissions(self.version_object, self.version_object)
        return self.version_object

    def get_extra_comment_data(self):
        return {
            'version_id': self.get_version_object().pk,
            'user': self.request.user.pk,
        }

    def filter_queryset(self, qset):
        qset = super().filter_queryset(qset)
        # Filter to only show your comments. We're already filtering on version
        # in get_queryset() as starting from the related manager allows us to
        # only load the version once.
        return qset.filter(user=self.request.user)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['version'] = self.get_version_object()
        # Patch in `version` and `user` as those are required by the serializer
        # and not provided by the API client as part of the POST data.
        self.request.data.update(self.get_extra_comment_data())
        return context


class ReviewAddonVersionCompareViewSet(
    ReviewAddonVersionMixin, RetrieveModelMixin, GenericViewSet
):
    def filter_queryset(self, qs):
        return qs.prefetch_related('files__validation')

    def get_objects(self):
        """Return a dict with both versions needed for the comparison,
        emulating what get_object() and get_version_object() do, but avoiding
        redundant queries.

        Dict keys are `instance` and `parent_version` for the main version
        object and the one to compare to, respectively."""
        pk = int(self.kwargs['pk'])
        parent_version_pk = int(self.kwargs['version_pk'])
        all_pks = set([pk, parent_version_pk])
        qs = self.filter_queryset(self.get_queryset())
        objs = qs.in_bulk(all_pks)
        if len(objs) != len(all_pks):
            # Return 404 if one of the objects requested failed to load. For
            # convenience in tests we allow self-comparaison even though it's
            # pointless, so check against len(all_pks) and not just `2`.
            raise http.Http404
        for obj in objs.values():
            self.check_object_permissions(self.request, obj)
        return {'instance': objs[pk], 'parent_version': objs[parent_version_pk]}

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['file'] = self.request.GET.get('file', None)

        if self.request.GET.get('file_only', 'false') == 'true':
            context['exclude_entries'] = True

        return context

    def get_serializer(self, instance=None, data=None, many=False, partial=False):
        context = self.get_serializer_context()
        context['parent_version'] = data['parent_version']

        if self.request.GET.get('file_only', 'false') == 'true':
            return AddonCompareVersionSerializerFileOnly(
                instance=instance, context=context
            )

        return AddonCompareVersionSerializer(instance=instance, context=context)

    def retrieve(self, request, *args, **kwargs):
        objs = self.get_objects()
        version = objs['instance']

        serializer = self.get_serializer(
            instance=version, data={'parent_version': objs['parent_version']}
        )
        return Response(serializer.data)


class CannedResponseViewSet(ListAPIView):
    permission_classes = [AllowAnyKindOfReviewer]

    queryset = CannedResponse.objects.all()
    serializer_class = CannedResponseSerializer
    # The amount of data will be small so that paginating will be
    # overkill and result in unnecessary additional requests
    pagination_class = None

    @classmethod
    def as_view(cls, **initkwargs):
        """The API is read-only so we can turn off atomic requests."""
        return non_atomic_requests(
            super(CannedResponseViewSet, cls).as_view(**initkwargs)
        )
