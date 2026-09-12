from django.urls import path
from . import privacy_views as views

urlpatterns = [
    path('patient/privacy/', views.PatientPrivacyView.as_view(), name='patient-privacy'),
    path('patient/data-requests/', views.PatientDataRequestListView.as_view(), name='patient-data-requests'),
    path('patient/data-requests/new/', views.PatientDataRequestCreateView.as_view(), name='patient-data-request-create'),
    path('patient/data-requests/<int:pk>/', views.PatientDataRequestDetailView.as_view(), name='patient-data-request-detail'),
    path('data-requests/', views.StaffDataRequestListView.as_view(), name='staff-data-requests'),
    path('data-requests/<int:pk>/', views.StaffDataRequestDetailView.as_view(), name='staff-data-request-detail'),
    path('account/access-history/', views.AccountAccessHistoryView.as_view(), name='account-access-history'),
    path('settings/policies/', views.PolicyListView.as_view(), name='policy-list'),
    path('settings/policies/new/', views.PolicyCreateView.as_view(), name='policy-create'),
    path('terms/', views.PublicPolicyView.as_view(page_kind='terms'), name='public-terms'),
    path('privacy/', views.PublicPolicyView.as_view(page_kind='privacy'), name='public-privacy'),
    path('contact/', views.PublicPolicyView.as_view(page_kind='contact'), name='public-contact'),
]
