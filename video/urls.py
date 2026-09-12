from django.urls import path

from . import views

app_name = 'video'

urlpatterns = [
    path('appointments/<int:appointment_id>/', views.room, name='room'),
    path('appointments/<int:appointment_id>/ice/', views.ice_config, name='ice-config'),
]
