"""The doctor's weight chart on the patient record. The patient app keeps its own, simpler chart.

Weight is drawn with the expected range (flat for the 28-day starter dose, then 0.5 to 1 kg a week
down from the first weight) and, once a goal is set, a projection at 0.7 kg a week to the goal.
Only doctors see the dose bars (one per shipment, scaled to the medicine's top dose) and the consult
dots, which open the consult note. Each medicine has its own colour and is named in the key; with one
medicine its doses are read off a right-hand mg axis, with more each bar is labelled where the dose changes.
"""

from datetime import date, timedelta
from decimal import Decimal
from math import ceil, floor

from django.utils.formats import date_format


WIDTH, HEIGHT = 560, 310
LEFT, RIGHT, TOP, BOTTOM = 34, 46, 18, 30
STARTER_DAYS = 28
PROJECTION_KG_PER_WEEK = 0.7
DOSE_ZONE = 0.35  # A medicine's top dose fills this share of the plot height, below most weights.
DOSE_TONES = 4  # Colours for medicines, in the order first shipped (record_overview.css).


def _f(value):
    return f'{value:.1f}'


def _mg(value):
    text = f'{Decimal(str(value)).normalize():f}'
    return text


def doctor_weight_chart(*, weights, goal=None, shipments=(), consults=(), medicines=()):
    """weights: (date, kg) pairs, oldest first. shipments: dicts with date, dose (Decimal mg), medicine and title,
    oldest first. medicines: dicts with name and dose_max (its top dose in the catalogue), in the order first
    shipped. consults: dicts with date, id and by. Returns None when no weight has been logged."""
    if not weights:
        return None
    first_day, start = weights[0][0], float(weights[0][1])
    last_day, last = weights[-1][0], float(weights[-1][1])
    goal = float(goal) if goal is not None else None
    to_goal = max(0.0, (last - goal) / PROJECTION_KG_PER_WEEK * 7) if goal is not None else 0.0
    end = last_day + timedelta(days=max(28, ceil(to_goal)))

    def days(day):
        return (day - first_day).days

    end_day = max(days(end), 1)
    values = [float(kg) for _, kg in weights]
    low = floor(min(values + ([goal] if goal is not None else [])) - 3)
    high = ceil(max(values) + 1.5)
    plot_width, plot_height, base = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM, HEIGHT - BOTTOM

    def x(day_number):
        return LEFT + day_number * plot_width / end_day

    def y(kg):
        return TOP + (high - kg) * plot_height / (high - low)

    def weeks(day_number):
        return max(0, day_number - STARTER_DAYS) / 7

    band = (f'M{_f(x(0))},{_f(y(start))} L{_f(x(STARTER_DAYS))},{_f(y(start))} '
            f'L{_f(x(end_day))},{_f(y(start - 0.5 * weeks(end_day)))} '
            f'L{_f(x(end_day))},{_f(y(start - 1.0 * weeks(end_day)))} L{_f(x(STARTER_DAYS))},{_f(y(start))} Z')
    grid = [{'y': _f(y(kg)), 'label_y': _f(y(kg) + 4), 'label': kg}
            for kg in range(ceil(low / 5) * 5, high + 1, 5)]

    points = [{'x': _f(x(days(day))), 'y': _f(y(float(kg))), 'title': f'{date_format(day, "j M Y")} · {_mg(kg)} kg'}
              for day, kg in weights]
    line = ' '.join(f'{"L" if index else "M"}{point["x"]},{point["y"]}' for index, point in enumerate(points))

    projection, projection_path = [], ''
    if goal is not None and to_goal > 0:
        step = 28
        while step < to_goal - 21:
            day = last_day + timedelta(days=step)
            projection.append((day, last - PROJECTION_KG_PER_WEEK * step / 7))
            step += 28
        projection.append((end, goal))
        projection = [{'x': _f(x(days(day))), 'y': _f(y(kg)), 'is_goal': index == len(projection) - 1,
                       'title': f'Projected {kg:.1f} kg · {date_format(day, "j M")}'}
                      for index, (day, kg) in enumerate(projection)]
        projection_path = f'M{points[-1]["x"]},{points[-1]["y"]} ' + ' '.join(f'L{p["x"]},{p["y"]}' for p in projection)

    names = list(dict.fromkeys([item['name'] for item in medicines] + [item.get('medicine', '') for item in shipments]))
    tones = {name: index % DOSE_TONES + 1 for index, name in enumerate(names)}
    tops = {item['name']: float(item['dose_max'] or 0) for item in medicines}
    for item in shipments:
        name = item.get('medicine', '')
        tops[name] = max(tops.get(name, 0), float(item['dose']))
    zone = plot_height * DOSE_ZONE

    def dose_y(mg, name):
        return base - mg / (tops.get(name) or 1) * zone

    bars, previous, shown = [], {}, []
    for item in shipments:
        day_number = days(item['date'])
        if not 0 <= day_number <= end_day:
            continue
        name = item.get('medicine', '')
        top = dose_y(float(item['dose']), name)
        label = _mg(item['dose'])
        bars.append({'x': _f(x(day_number) - 7), 'y': _f(top), 'height': _f(base - top), 'label_x': _f(x(day_number)),
                     'label_y': _f(top - 4), 'label': label if label != previous.get(name) else '', 'title': item['title'],
                     'tone': tones[name]})
        previous[name] = label
        if name not in shown:
            shown.append(name)
    single = shown[0] if len(shown) == 1 else None
    dose_axis = [{'y': _f(dose_y(mg, single)), 'label_y': _f(dose_y(mg, single) + 3.5), 'label': _mg(round(mg, 2))}
                 for mg in (0, tops[single] / 2, tops[single])] if single is not None else []

    months, cursor = [], date(first_day.year, first_day.month, 1)
    while cursor <= end:
        if cursor >= first_day:
            months.append({'x': _f(x(days(cursor))), 'label': date_format(cursor, 'M')})
        cursor = date(cursor.year + cursor.month // 12, cursor.month % 12 + 1, 1)

    dots = [{'x': _f(x(days(item['date']))), 'id': item['id'],
             'label': f'Consultation {date_format(item["date"], "j M Y")} with {item["by"]}. Open the note.'}
            for item in consults if 0 <= days(item['date']) <= end_day]

    return {
        'width': WIDTH, 'height': HEIGHT, 'left': LEFT, 'right_edge': WIDTH - RIGHT, 'top': TOP, 'base': base,
        'plot_width': plot_width, 'plot_height': plot_height, 'label_y': HEIGHT - 8, 'kg_label_y': TOP - 6,
        'band': band, 'grid': grid, 'line': line, 'points': points,
        'projection': projection, 'projection_path': projection_path,
        'future_x': points[-1]['x'], 'future_width': _f(x(end_day) - float(points[-1]['x'])),
        'goal': goal, 'goal_y': _f(y(goal)) if goal is not None else None,
        'goal_label': f'Goal {_mg(goal)} kg' if goal is not None else '',
        'goal_label_x': WIDTH - RIGHT - 10, 'goal_label_y': _f(y(goal) - 6) if goal is not None else None,
        'goal_pill': f'Goal {_mg(goal)} kg · around {date_format(end, "j M")}' if goal is not None else '',
        'bars': bars, 'dose_axis': dose_axis, 'dose_tone': tones[single] if single is not None else 1,
        'medicines': [{'name': name, 'tone': tones[name]} for name in sorted(shown, key=names.index)], 'dose_axis_top': _f(base - zone), 'dose_axis_label_y': _f(base - zone - 10),
        'dose_axis_x': WIDTH - RIGHT + 6, 'dose_tick_end': WIDTH - RIGHT + 4, 'dose_label_x': WIDTH - RIGHT + 7,
        'months': months, 'month_tick_end': base + 4, 'consults': dots,
        'summary': f'Weight {_mg(weights[0][1])} to {_mg(weights[-1][1])} kg' + (', with doses shipped' if bars else '')
                   + (' and consult dates' if dots else ''),
    }
