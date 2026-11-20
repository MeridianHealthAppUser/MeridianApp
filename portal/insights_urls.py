from django.urls import path

from .insights_views import CohortsView, InsightFiguresView, InsightNoteView, InsightsView

urlpatterns = [
    path('metrics/', InsightsView.as_view(), name='metrics'),
    path('metrics/export.xlsx', InsightsView.as_view(export=True), name='metrics-export'),
    path('metrics/cohorts/', CohortsView.as_view(), name='insights-cohorts'),
    path('metrics/notes/', InsightNoteView.as_view(), name='insight-notes'),
    path('metrics/notes/<int:pk>/delete/', InsightNoteView.as_view(), name='insight-note-delete'),
    path('metrics/figures/', InsightFiguresView.as_view(), name='insight-figures'),
]
