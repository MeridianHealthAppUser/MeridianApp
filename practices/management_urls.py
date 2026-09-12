from django.urls import path

from .management_views import (ManagementMembershipEditView, ManagementPracticeCreateView,
    ManagementPracticeEditView, ManagementPracticesView, ManagementUserCreateView, ManagementUsersView)

# Included inside portal.urls so all workspace links keep the portal namespace.
urlpatterns = [
    path('settings/users/', ManagementUsersView.as_view(), name='management-users'),
    path('settings/users/new/', ManagementUserCreateView.as_view(), name='management-user-create'),
    path('settings/users/<int:pk>/', ManagementMembershipEditView.as_view(), name='management-membership-edit'),
    path('settings/practices/', ManagementPracticesView.as_view(), name='management-practices'),
    path('settings/practices/new/', ManagementPracticeCreateView.as_view(), name='management-practice-create'),
    path('settings/practices/<int:pk>/', ManagementPracticeEditView.as_view(), name='management-practice-edit'),
]
