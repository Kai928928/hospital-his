from datetime import date, datetime, time


PERIODS = {'am': '上午 08:00-12:00', 'pm': '下午 14:00-18:00'}
PERIOD_END = {'am': time(12), 'pm': time(18)}


def schedule_has_ended(schedule, now=None):
    now = now or datetime.now()
    return (schedule.visit_date < now.date() or
            (schedule.visit_date == now.date() and now.time() >= PERIOD_END[schedule.period]))


def schedule_available(schedule):
    return schedule.is_open and not schedule_has_ended(schedule) and schedule.booked_count < schedule.capacity


def visit_date(value):
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise ValueError('请选择有效的就诊日期') from None
