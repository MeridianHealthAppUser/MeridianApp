"""Chart geometry over already-authorised aggregate data; no database access."""
from math import ceil


def share(count, total):
    return round(count * 100 / total, 1) if total else 0


def metrics_dashboard(results):
    values, data = dict(results['metrics']), results['analytics']
    appointments = values['Appointments scheduled in period']
    kpis = [
        ('Active patients', values['Active patient records now'], 'Current snapshot',
         f"{values['New patient records in period']} new records in period", 'teal'),
        ('Active plans', values['Active local plans now'], 'Current snapshot',
         f"{values['Paused plans now']} paused now", 'blue'),
        ('Appointments', appointments, 'Selected period',
         f"{values['Completed appointments in period']} completed", 'violet'),
        ('Dispatches', values['Dispatches recorded in period'], 'Selected period',
         f"{values['Delivered from those dispatches']} delivered from these dispatches", 'amber'),
    ]
    daily = data['daily']
    maximum = max((row[key] for row in daily for key in ('new_patients', 'appointments', 'dispatches')), default=0)
    ceiling = max(1, ceil(maximum / 4)) * 4
    lines = []
    for key, label, tone in (('new_patients', 'New patient records', 'teal'),
                             ('appointments', 'Appointments', 'blue'), ('dispatches', 'Dispatches', 'amber')):
        points = [dict(x=f'{(index * 600 / (len(daily)-1) if len(daily)>1 else 300):.2f}',
                       y=f'{180 - row[key] * 180 / ceiling:.2f}', date=row['date'], value=row[key])
                  for index, row in enumerate(daily)]
        lines.append(dict(label=label, tone=tone, points=points, total=sum(row[key] for row in daily),
                          polyline=' '.join(f'{point["x"]},{point["y"]}' for point in points)))
    statuses, stops, offset = [], [], 0
    colors = {'booked': '#149b90', 'completed': '#5a85cc', 'no_show': '#9b87ba', 'cancelled': '#e5af65'}
    for item in data['appointment_status']:
        color = colors.get(item['key'], '#899d9e')
        end = offset + (item['count'] * 100 / appointments if appointments else 0)
        stops.append(f'{color} {offset:.4f}% {end:.4f}%')
        statuses.append(dict(item, share=share(item['count'], appointments), color=color))
        offset = end
    cohort_max = max((row['records'] for row in results['cohorts']), default=0)
    cohorts = [dict(row, width=share(row['records'], cohort_max)) for row in results['cohorts']]
    distribution = data['weight_distribution']
    weight_rows = [dict(label=label, count=distribution[key], width=share(distribution[key], distribution['count']), tone=tone)
                   for key, label, tone in (('decrease', 'Lower recorded weight', 'teal'),
                                            ('unchanged', 'Unchanged', 'muted'), ('increase', 'Higher recorded weight', 'blue'))]
    operations = [('Available stock', values['Unexpired available stock units now'], 'Unexpired units now'),
                  ('Lab requests', values['Lab requests created in period'], 'Created in period'),
                  ('Labs reviewed', values['Those lab requests reviewed'], 'From those requests')]
    if 'New enquiries in practices you administer' in values:
        operations.append(('New enquiries', values['New enquiries in practices you administer'],
                           f"{values['Converted enquiries from that group']} converted · administered practices only"))
    return dict(kpis=kpis, lines=lines, has_activity=maximum > 0, daily=daily,
                ticks=[ceiling - i * ceiling // 4 for i in range(5)], show_points=len(daily) <= 31,
                middle=daily[len(daily)//2]['date'] if daily else None,
                appointment_total=appointments, statuses=statuses,
                ring=f'conic-gradient({", ".join(stops)})' if appointments else '#eaf0ef',
                cohorts=cohorts, cohort_total=sum(row['records'] for row in cohorts),
                weight_rows=weight_rows, operations=operations)
