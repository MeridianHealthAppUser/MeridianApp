from django.urls import path
from . import privacy_views as views

urlpatterns = [
    # My profile: personal details, security, preferences, consents and data requests on one page.
    path('patient/profile/', views.PatientProfileView.as_view(), name='patient-profile'),
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
