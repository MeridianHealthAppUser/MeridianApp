from django.urls import path

from .record_views import ClinicalRecordExportView, ClinicalRecordView


urlpatterns = [
    path('patients/<int:pk>/record/', ClinicalRecordView.as_view(), name='staff-patient-record'),
    path('patients/<int:pk>/record/export/', ClinicalRecordExportView.as_view(), name='staff-patient-record-export'),
]
