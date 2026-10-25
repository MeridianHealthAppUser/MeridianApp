from django.urls import path

from . import journey_views as views

urlpatterns = [
    path('settings/email-journeys/', views.JourneyListView.as_view(), name='email-journeys'),
    path('settings/email-journeys/new/', views.JourneyEditView.as_view(), name='email-journey-create'),
    path('settings/email-journeys/<int:pk>/', views.JourneyEditView.as_view(), name='email-journey-edit'),
    path('emails/', views.EmailLogView.as_view(), name='email-log'),
    path('email/unsubscribe/<str:token>/', views.UnsubscribeView.as_view(), name='email-unsubscribe'),
]
