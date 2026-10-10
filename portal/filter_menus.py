"""What goes in a Filters menu (templates/portal/includes/filter_menu.html): its sections, and where Apply goes.

A section ticks any of its choices, picks one (pick=True), or holds a few fields such as dates. Its count is how many
filters it applies, which add up to the number on the Filters button; a pick that's on its default doesn't count.
"""


def filter_menu(*, action, sections, clear_url, label, hidden=(), form=None):
    return dict(action=action, sections=sections, clear_url=clear_url, label=label, hidden=list(hidden),
                count=sum(section['count'] for section in sections), invalid=bool(form is not None and form.errors))


def choice_section(name, label, choices, ticked=(), *, icon='status', pick=False, default=None, errors=()):
    ticked = {str(value) for value in ticked}
    options = [dict(value=str(value), label=text, checked=str(value) in ticked) for value, text in choices]
    if pick:
        count = int(any(option['checked'] and option['value'] != str(default) for option in options))
    else:
        count = sum(option['checked'] for option in options)
    return dict(name=name, label=label, icon=icon, kind='pick' if pick else 'choices', options=options,
                count=count, errors=list(errors))


def fields_section(name, label, fields, *, icon='date', count=0):
    return dict(name=name, label=label, icon=icon, kind='fields', fields=fields, count=count,
                errors=[error for field in fields for error in field.errors])


def ticked_values(values):
    """What's ticked in a section, from repeated fields or one comma-separated field (as forms that keep a filter send it)."""
    values = [values] if isinstance(values, str) else (values or [])
    return [part for item in values for part in str(item).split(',') if part]


def one_ticked(values, choices):
    """The one choice ticked in a two-way section such as Open or Closed. Ticking both is the same as ticking neither,
    so either way there's no filter. Anything else, such as the old "all", is ignored."""
    chosen = set(ticked_values(values))
    picked = [value for value, _ in choices if value in chosen]
    return picked if len(picked) == 1 else []


def ticked_ids(values):
    """Record ids ticked in a section, such as patients; anything that isn't one is ignored."""
    return sorted({int(value) for value in ticked_values(values) if value.isascii() and value.isdecimal() and len(value) < 19})
