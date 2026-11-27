from django.urls import path

from . import screening_views as views

urlpatterns = [
    path('settings/screening/', views.ScreeningOverviewView.as_view(), name='screening-questions'),
    path('settings/screening/versions/<int:number>/', views.ScreeningVersionView.as_view(), name='screening-version'),
    path('settings/screening/draft/start/', views.DraftStartView.as_view(), name='screening-draft-start'),
    path('settings/screening/draft/', views.DraftEditorView.as_view(), name='screening-draft'),
    path('settings/screening/draft/questions/new/', views.QuestionAddView.as_view(), name='screening-question-add'),
    path('settings/screening/draft/questions/<slug:key>/', views.QuestionSaveView.as_view(), name='screening-question-save'),
    path('settings/screening/draft/questions/<slug:key>/retire/', views.QuestionRetireView.as_view(), name='screening-question-retire'),
    path('settings/screening/draft/order/', views.ReorderView.as_view(), name='screening-reorder'),
    path('settings/screening/draft/publish/', views.PublishView.as_view(), name='screening-publish'),
    path('settings/screening/draft/discard/', views.DiscardView.as_view(), name='screening-discard'),
    path('settings/screening/preview/<int:number>/', views.ScreeningPreviewView.as_view(), name='screening-preview'),
]
