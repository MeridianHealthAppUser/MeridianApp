from django.contrib import admin

from care.admin import OperationalWorkflowReadOnlyAdmin
from .models import CallParticipant, CallSession


class CallParticipantInline(admin.TabularInline):
    model = CallParticipant
    extra = 0
    can_delete = False
    fields = readonly_fields = ('user', 'joined_at', 'last_seen_at', 'lease_expires_at', 'ended_at', 'end_reason')

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CallSession)
class CallSessionAdmin(OperationalWorkflowReadOnlyAdmin):
    list_display = ('appointment', 'company', 'started_at', 'last_seen_at', 'lease_expires_at', 'ended_at')
    inlines = (CallParticipantInline,)


@admin.register(CallParticipant)
class CallParticipantAdmin(OperationalWorkflowReadOnlyAdmin):
    list_display = ('session', 'company', 'user', 'joined_at', 'lease_expires_at', 'ended_at', 'end_reason')
