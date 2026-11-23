"""Money as purchase orders print it."""
from django import template

from care.purchasing import format_rand

register = template.Library()


@register.filter
def rand(value):
    """R 1 600.00, or an en dash when there is no amount."""
    return '–' if value is None or value == '' else format_rand(value)
