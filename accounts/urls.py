from django.urls import path

from .role_views import PracticeRoleView
from .views import (
    AccountLoginView, AccountLogoutView, AccountPasswordChangeDoneView, AccountPasswordChangeView,
    AccountPasswordResetCompleteView, AccountPasswordResetConfirmView, AccountPasswordResetDoneView,
    AccountPasswordResetView, AccountProfileView, AccountSignOutOthersView,
)


app_name = 'accounts'

urlpatterns = [
    path('login/', AccountLoginView.as_view(), name='login'),
    path('logout/', AccountLogoutView.as_view(), name='logout'),
    path('profile/', AccountProfileView.as_view(), name='profile'),
    path('profile/sign-out-others/', AccountSignOutOthersView.as_view(), name='sign-out-others'),
    path('practice-role/', PracticeRoleView.as_view(), name='practice-role'),
    path('password/change/', AccountPasswordChangeView.as_view(), name='password-change'),
    path('password/changed/', AccountPasswordChangeDoneView.as_view(), name='password-change-done'),
    path('password/forgot/', AccountPasswordResetView.as_view(), name='password-reset'),
    path('password/forgot/sent/', AccountPasswordResetDoneView.as_view(), name='password-reset-done'),
    path('password/reset/<uidb64>/<token>/', AccountPasswordResetConfirmView.as_view(), name='password-reset-confirm'),
    path('password/reset/done/', AccountPasswordResetCompleteView.as_view(), name='password-reset-complete'),
]
