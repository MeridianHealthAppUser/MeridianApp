"""Numbers on Insights: thousands spaced, as in South Africa (12 257), and Rand with no cents (R12 257)."""

from django import template

register = template.Library()


@register.filter
def grouped(value):
    if value is None or value == '':
        return '—'
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if number.is_integer():
        return f'{int(number):,}'.replace(',', ' ')
    return f'{number:,.1f}'.replace(',', ' ')


@register.filter
def rand(value):
    if value is None or value == '':
        return '—'
    try:
        return f'R{round(float(value)):,}'.replace(',', ' ')
    except (TypeError, ValueError):
        return value
