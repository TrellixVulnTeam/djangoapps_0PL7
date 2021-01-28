"""website URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/2.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf.urls import url
from django.contrib import admin
from django.urls import path, include, re_path
from django.views.static import serve

from rest_framework.documentation import include_docs_urls

from rest_framework_jwt.views import obtain_jwt_token, refresh_jwt_token

from apps.article.views import ArticleCreated
from apps.course.views import CoursesList, CourseCreatedList, CourseListCreated, MeCoursesList
from apps.dynamic .views import Home
from apps.forum.views import Forum_plateView, ForumView, CommentView, Parent_CommentView, ForumListView
from apps.support.views import LinkList, EmailsList, BannerList, QQList, SeoList
from apps.user.views import test, captcha_refresh, yan, login_view, UserGetInfo, UserGetAllInfo, \
    PersonOthers, Register, active_user, get_message, UserMessages, qq, getClback, getClbackQQ, UserFollows, AppMessage, \
    UserFollowOther
from django.views.generic import TemplateView

from website import settings
from apps.article import views
from apps.user.views import logout_view, Person, PersonApi,to_login
from rest_framework import routers

router = routers.DefaultRouter()
router.register('article_list', views.ArticleListView)
router.register('me_article_list', views.MeArticleListView)
router.register('ArticleCommit', views.ArticleCommit)
router.register('follow_list', views.FollowListView)
router.register('category', views.CategoryView)
router.register('article_Comment', views.ArticleCommintView)
router.register('comment_reply', views.ArticleCommentReplyView)
router.register('PersonApi', PersonApi)
router.register('apiinfo', UserGetInfo)
router.register('all_info', UserGetAllInfo)
# router.register('user_disbale', UserDisbale)
router.register('PersonOthers', PersonOthers)
router.register('UserMessages', UserMessages, base_name='UserMessages')
router.register('article', ArticleCreated)
router.register('courseList', CoursesList)
router.register('mecourseList', MeCoursesList)
router.register('course', CourseCreatedList)
router.register('Addtutorial', CourseListCreated)
router.register('BannerList', BannerList)
router.register('EmailsList', EmailsList)
router.register('LinkList', LinkList)
router.register('forum/category', Forum_plateView)
router.register('forum', ForumView)
router.register('CommentView', CommentView)
router.register('Parent_CommentView', Parent_CommentView)
router.register('get-list',QQList)
router.register('seo-list',SeoList,basename='seo-list')
router.register('UserFollows',UserFollows)
router.register('AppMessage',AppMessage)
router.register('UserFollowOther',UserFollowOther)
router.register('ForumListView',ForumListView)

urlpatterns = [

    #path('admins/', admin.site.urls),
    path('admin/', TemplateView.as_view(template_name='admin/index.html')),
   # path('404/', TemplateView.as_view(template_name='500.html')),
    # path('',test), # 这是生成验证码的图片
    url(r'^captcha/', include('captcha.urls')),
    path('refresh/', captcha_refresh),  # 这是生成验证码的图片
    path('yan/', yan),  # 这是生成验证码的图片
    # path('', Home, name='home'),
    path('', views.Home, name='home'),

    path('webapp/', TemplateView.as_view(template_name='webapp/index.html')),
    path('login/', login_view, name='index'),
    path('info/', get_message, name='info'),
    path('person/', include('apps.user.urls')),
    path('logou/', logout_view, name='logou'),
    path('register/', Register.as_view(), name='register'),
    path('article/', include('apps.article.urls')),
    path('course/', include('apps.course.urls')),
    path('support/', include('apps.support.urls')),
    path('forum/', include('apps.forum.urls')),
    path('ads.txt/',TemplateView.as_view(template_name='ads.txt')),
    path('root.txt/', TemplateView.as_view(template_name='root.txt')),
    path('jd_root.txt/', TemplateView.as_view(template_name='jd_root.txt')),
    path('gome_20943.txt/', TemplateView.as_view(template_name='gome_20943.txt')),
    #url(r'^activate/(?P<token>\w+.[-_\w]*\w+.[-_\w]*\w+)/$', active_user, name='active_user'),
    path('activate/<str:token>', active_user, name='active_user'),
    url(r'^search/', include('haystack.urls'), name='haystack_search'),

    re_path(r'^upload/(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT}),
    url(r'api/', include(router.urls)),
    url(r'^api-auth/', include('rest_framework.urls', namespace='rest_framework')),
    path("api-docs/", include_docs_urls("API文档")),
    re_path(r'api/login/$', obtain_jwt_token),  # jwt认证
    re_path(r'^api-token-refresh/', refresh_jwt_token),#jwt刷新
    url('auth-qq', to_login, name='qq-login'),
    url('qq', qq, name='qq'),
    url('callbackget', getClback, name='callbackget'),
    url('getClbackQQ', getClbackQQ, name='getClbackQQ'),
    re_path(r'^static/(?P<path>.*)$', serve, {'document_root': settings.STATIC_ROOT})  # 配置文件上传html显示
]

#全局404
handler404='apps.user.views.page_not_found'
handler500='apps.user.views.page_error'