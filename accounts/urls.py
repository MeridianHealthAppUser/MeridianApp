from django.urls import path

from .role_views import PracticeRoleView
from .views import AccountLoginView, AccountLogoutView, AccountPasswordChangeView, AccountPasswordChangeDoneView, AccountProfileView


app_name = 'accounts'

urlpatterns = [
    path('login/', AccountLoginView.as_view(), name='login'),
    path('logout/', AccountLogoutView.as_view(), name='logout'),
    path('profile/', AccountProfileView.as_view(), name='profile'),
    path('practice-role/', PracticeRoleView.as_view(), name='practice-role'),
    path('password/change/', AccountPasswordChangeView.as_view(), name='password-change'),
    path('password/changed/', AccountPasswordChangeDoneView.as_view(), name='password-change-done'),
]
