from django.urls import path
from .reporting_views import (ActivityReportDetailView, ActivityReportSendView, ActivityReportSettingsView,
                              StatementCreateView, StatementDetailView, StatementListView)

urlpatterns = [
    path('activity-statements/', StatementListView.as_view(), name='activity-statements'),
    path('activity-statements/new/', StatementCreateView.as_view(), name='activity-statement-create'),
    path('activity-statements/<int:pk>/', StatementDetailView.as_view(), name='activity-statement-detail'),
    path('activity-statements/<int:pk>/export.csv', StatementDetailView.as_view(export=True), name='activity-statement-export'),
    # The monthly activity report (care.activity_reports), for the Super Admin.
    path('activity-statements/monthly/settings/', ActivityReportSettingsView.as_view(), name='activity-report-settings'),
    path('activity-statements/monthly/send/', ActivityReportSendView.as_view(), name='activity-report-send'),
    path('activity-statements/monthly/<int:pk>/', ActivityReportDetailView.as_view(), name='activity-report-detail'),
    path('activity-statements/monthly/<int:pk>/export.csv', ActivityReportDetailView.as_view(export=True), name='activity-report-export'),
]
