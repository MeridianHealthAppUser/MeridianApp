from django.urls import path

from . import treatment_views as views
from . import review_views


urlpatterns = [
    path('review-rules/', review_views.ReviewRuleListView.as_view(), name='treatment-review-rules'),
    path('review-rules/new/', review_views.ReviewRuleEditorView.as_view(), name='treatment-review-rule-create'),
    path('review-rules/default/', review_views.ReviewDefaultUpdateView.as_view(), name='treatment-review-default'),
    path('review-rules/<int:pk>/', review_views.ReviewRuleEditorView.as_view(), name='treatment-review-rule-detail'),
    path('authorisations/', views.AuthorizationListView.as_view(), name='treatment-authorisations'),
    path('patients/<int:patient_pk>/authorisations/new/', views.AuthorizationEditorView.as_view(), name='treatment-authorisation-create'),
    path('authorisations/<int:pk>/', views.AuthorizationDetailView.as_view(), name='treatment-authorisation-detail'),
    path('authorisations/<int:pk>/renew/', views.AuthorizationEditorView.as_view(), name='treatment-authorisation-renew'),
    path('authorisations/<int:pk>/status/', views.AuthorizationStatusView.as_view(), name='treatment-authorisation-status'),
    path('subscriptions/', views.SubscriptionListView.as_view(), name='treatment-subscriptions'),
    path('patient/treatment/', views.PatientTreatmentView.as_view(), name='patient-treatment'),
    path('patient/subscription/', views.PatientSubscriptionView.as_view(), name='patient-subscription'),
    path('patient/subscription/enroll/', views.PatientEnrollmentView.as_view(), name='patient-subscription-enroll'),
    path('patient/subscription/<int:pk>/status/', views.PatientSubscriptionStatusView.as_view(), name='patient-subscription-status'),
]
