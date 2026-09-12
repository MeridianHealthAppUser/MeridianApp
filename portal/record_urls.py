from django.urls import path

from .record_views import ClinicalRecordExportView, ClinicalRecordView
from .record_history import ClinicalRecordExcelView, ClinicalRecordHistoryView


urlpatterns = [
    path('patients/<int:pk>/record/', ClinicalRecordView.as_view(), name='staff-patient-record'),
    path('patients/<int:pk>/record/export/', ClinicalRecordExportView.as_view(), name='staff-patient-record-export'),
    path('patients/<int:pk>/record/history/', ClinicalRecordHistoryView.as_view(), name='staff-patient-record-history'),
    path('patients/<int:pk>/record/export.xlsx', ClinicalRecordExcelView.as_view(), name='staff-patient-record-excel'),
]
