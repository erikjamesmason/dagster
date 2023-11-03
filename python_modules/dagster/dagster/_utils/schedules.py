import calendar
import datetime
import functools
from typing import Iterator, Optional, Sequence, Union

import pendulum
import pytz
from croniter import croniter as _croniter

import dagster._check as check
from dagster._core.definitions.partition import ScheduleType
from dagster._seven.compat.pendulum import PendulumDateTime, create_pendulum_time, to_timezone


class CroniterShim(_croniter):
    """Lightweight shim to enable caching certain values that may be calculated many times."""

    @classmethod
    @functools.lru_cache(maxsize=128)
    def expand(cls, *args, **kwargs):
        return super().expand(*args, **kwargs)


def _exact_match(cron_expression: str, dt: datetime.datetime) -> bool:
    """The default croniter match function only checks that the given datetime is within 60 seconds
    of a cron schedule tick. This function checks that the given datetime is exactly on a cron tick.
    """
    if (
        cron_expression == "0 0 * * *"
        and dt.hour == 0
        and dt.minute == 0
        and dt.second == 0
        and dt.microsecond == 0
    ):
        return True

    if cron_expression == "0 * * * *" and dt.minute == 0 and dt.second == 0 and dt.microsecond == 0:
        return True

    cron = CroniterShim(
        cron_expression, dt + datetime.timedelta(microseconds=1), ret_type=datetime.datetime
    )
    return dt == cron.get_prev()


def is_valid_cron_string(cron_string: str) -> bool:
    if not CroniterShim.is_valid(cron_string):
        return False
    # Croniter < 1.4 returns 2 items
    # Croniter >= 1.4 returns 3 items
    expanded, *_ = CroniterShim.expand(cron_string)
    # dagster only recognizes cron strings that resolve to 5 parts (e.g. not seconds resolution)
    return len(expanded) == 5


def is_valid_cron_schedule(cron_schedule: Union[str, Sequence[str]]) -> bool:
    return (
        is_valid_cron_string(cron_schedule)
        if isinstance(cron_schedule, str)
        else len(cron_schedule) > 0
        and all(is_valid_cron_string(cron_string) for cron_string in cron_schedule)
    )


def _replace_date_fields(
    pendulum_date: PendulumDateTime,
    hour: int,
    minute: int,
    day: int,
):
    try:
        new_time = create_pendulum_time(
            pendulum_date.year,
            pendulum_date.month,
            day,
            hour,
            minute,
            0,
            0,
            tz=pendulum_date.timezone_name,
            dst_rule=pendulum.TRANSITION_ERROR,
        )
    except pendulum.tz.exceptions.NonExistingTime:  # type: ignore
        # If we fall on a non-existant time (e.g. between 2 and 3AM during a DST transition)
        # advance to the end of the window, which does exist - match behavior described in the docs:
        # https://docs.dagster.io/concepts/partitions-schedules-sensors/schedules#execution-time-and-daylight-savings-time)
        new_time = create_pendulum_time(
            pendulum_date.year,
            pendulum_date.month,
            day,
            hour + 1,
            0,
            0,
            0,
            tz=pendulum_date.timezone_name,
            dst_rule=pendulum.TRANSITION_ERROR,
        )
    except pendulum.tz.exceptions.AmbiguousTime:  # type: ignore
        # For consistency, always choose the latter of the two possible times during a fall DST
        # transition when there are two possibilities - match behavior described in the docs:
        # https://docs.dagster.io/concepts/partitions-schedules-sensors/schedules#execution-time-and-daylight-savings-time)
        new_time = create_pendulum_time(
            pendulum_date.year,
            pendulum_date.month,
            day,
            hour,
            minute,
            0,
            0,
            tz=pendulum_date.timezone_name,
            dst_rule=pendulum.POST_TRANSITION,
        )

    return new_time


def _find_previous_schedule_time(
    minute: Optional[int],
    hour: Optional[int],
    day: Optional[int],
    day_of_week: Optional[int],
    schedule_type: ScheduleType,
    pendulum_date: PendulumDateTime,
) -> PendulumDateTime:
    if schedule_type == ScheduleType.HOURLY:
        new_timestamp = int(pendulum_date.timestamp())
        new_timestamp = new_timestamp - new_timestamp % 60

        current_minute = pendulum_date.minute
        new_timestamp = new_timestamp - 60 * ((current_minute - check.not_none(minute)) % 60)

        if new_timestamp >= pendulum_date.timestamp():
            new_timestamp = new_timestamp - 60 * 60

        return pendulum.from_timestamp(new_timestamp, tz=pendulum_date.timezone_name)
    elif schedule_type == ScheduleType.DAILY:
        # First move to the correct time of day today (ignoring whether it is the correct day)
        new_time = _replace_date_fields(
            pendulum_date,
            check.not_none(hour),
            check.not_none(minute),
            pendulum_date.day,
        )

        if new_time.timestamp() >= pendulum_date.timestamp():
            # Move back a day if needed
            new_time = new_time.subtract(days=1)

            # Doing so may have adjusted the hour again if we crossed a DST boundary,
            # so make sure it's still correct
            new_time = _replace_date_fields(
                new_time,
                check.not_none(hour),
                check.not_none(minute),
                new_time.day,
            )

        return new_time
    elif schedule_type == ScheduleType.WEEKLY:
        # first move to the correct time of day
        new_time = _replace_date_fields(
            pendulum_date,
            check.not_none(hour),
            check.not_none(minute),
            pendulum_date.day,
        )

        # Go back far enough to make sure that we're now on the correct day of the week
        current_day_of_week = new_time.day_of_week
        if day_of_week != current_day_of_week:
            new_time = new_time.subtract(days=(current_day_of_week - day_of_week) % 7)

        # Make sure that we've actually moved back, go back a week if we haven't
        if new_time.timestamp() >= pendulum_date.timestamp():
            new_time = new_time.subtract(weeks=1)

        # Doing so may have adjusted the hour again if we crossed a DST boundary,
        # so make sure the time is still correct
        new_time = _replace_date_fields(
            new_time,
            check.not_none(hour),
            check.not_none(minute),
            new_time.day,
        )

        return new_time

    elif schedule_type == ScheduleType.MONTHLY:
        # First move to the correct day and time of day
        new_time = _replace_date_fields(
            pendulum_date,
            check.not_none(hour),
            check.not_none(minute),
            check.not_none(day),
        )

        if new_time.timestamp() >= pendulum_date.timestamp():
            # Move back a month if needed
            new_time = new_time.subtract(months=1)

            # Doing so may have adjusted the hour again if we crossed a DST boundary,
            # so make sure it's still correct
            new_time = _replace_date_fields(
                new_time,
                check.not_none(hour),
                check.not_none(minute),
                check.not_none(day),
            )

        return new_time
    else:
        raise Exception(f"Unexpected schedule type {schedule_type}")


def cron_string_iterator(
    start_timestamp: float,
    cron_string: str,
    execution_timezone: Optional[str],
    start_offset: int = 0,
) -> Iterator[datetime.datetime]:
    """Generator of datetimes >= start_timestamp for the given cron string."""
    # leap day special casing
    if cron_string.endswith(" 29 2 *"):
        min_hour, _ = cron_string.split(" 29 2 *")
        day_before = f"{min_hour} 28 2 *"
        # run the iterator for Feb 28th
        for dt in cron_string_iterator(
            start_timestamp=start_timestamp,
            cron_string=day_before,
            execution_timezone=execution_timezone,
            start_offset=start_offset,
        ):
            # only return on leap years
            if calendar.isleap(dt.year):
                # shift 28th back to 29th
                shifted_dt = dt + datetime.timedelta(days=1)
                yield shifted_dt
        return

    timezone_str = execution_timezone if execution_timezone else "UTC"

    # Croniter < 1.4 returns 2 items
    # Croniter >= 1.4 returns 3 items
    cron_parts, nth_weekday_of_month, *_ = CroniterShim.expand(cron_string)

    is_numeric = [len(part) == 1 and part[0] != "*" for part in cron_parts]
    is_wildcard = [len(part) == 1 and part[0] == "*" for part in cron_parts]

    known_schedule_type: Optional[ScheduleType] = None

    delta_fn = None
    should_hour_change = False
    expected_hour = None
    expected_minute = None
    expected_day = None
    expected_day_of_week = None

    # Special-case common intervals (hourly/daily/weekly/monthly) since croniter iteration can be
    # much slower than adding a fixed interval
    if not nth_weekday_of_month:
        if all(is_numeric[0:3]) and all(is_wildcard[3:]):  # monthly
            delta_fn = lambda d, num: d.add(months=num)
            should_hour_change = False
            known_schedule_type = ScheduleType.MONTHLY
        elif all(is_numeric[0:2]) and is_numeric[4] and all(is_wildcard[2:4]):  # weekly
            delta_fn = lambda d, num: d.add(weeks=num)
            should_hour_change = False
            known_schedule_type = ScheduleType.WEEKLY
        elif all(is_numeric[0:2]) and all(is_wildcard[2:]):  # daily
            delta_fn = lambda d, num: d.add(days=num)
            should_hour_change = False
            known_schedule_type = ScheduleType.DAILY
        elif is_numeric[0] and all(is_wildcard[1:]):  # hourly
            delta_fn = lambda d, num: d.add(hours=num)
            should_hour_change = True
            known_schedule_type = ScheduleType.HOURLY

    if is_numeric[1]:
        expected_hour = int(cron_parts[1][0])

    if is_numeric[0]:
        expected_minute = int(cron_parts[0][0])

    if is_numeric[2]:
        expected_day = int(cron_parts[2][0])

    if is_numeric[4]:
        expected_day_of_week = int(cron_parts[4][0])

    date_iter: Optional[CroniterShim] = None

    # Croniter doesn't behave nicely with pendulum timezones
    utc_datetime = pytz.utc.localize(datetime.datetime.utcfromtimestamp(start_timestamp))
    start_datetime = utc_datetime.astimezone(pytz.timezone(timezone_str))

    date_iter = CroniterShim(cron_string, start_datetime)

    if delta_fn is not None and start_offset == 0 and _exact_match(cron_string, start_datetime):
        # In simple cases, where you're already on a cron boundary, the below logic is unnecessary
        # and slow
        next_date = start_datetime
        # This is already on a cron boundary, so yield it
        yield to_timezone(pendulum.instance(next_date), timezone_str)

    elif known_schedule_type:
        # This logic working correctly requires a pendulum datetime to ensure that we are tracking
        # corretly which side of a DST transition we are on
        pendulum_datetime = pendulum.from_timestamp(start_timestamp, tz=timezone_str)
        next_date = _find_previous_schedule_time(
            expected_minute,
            expected_hour,
            expected_day,
            expected_day_of_week,
            known_schedule_type,
            pendulum_datetime,
        )

        check.invariant(start_offset <= 0)
        for _ in range(-start_offset):
            next_date = _find_previous_schedule_time(
                expected_minute,
                expected_hour,
                expected_day,
                expected_day_of_week,
                known_schedule_type,
                next_date,
            )
    else:
        # Go back one iteration so that the next iteration is the first time that is >= start_datetime
        # and matches the cron schedule
        next_date = date_iter.get_prev(datetime.datetime)

        if not CroniterShim.match(cron_string, next_date):
            # Workaround for upstream croniter bug where get_prev sometimes overshoots to a time
            # that doesn't actually match the cron string (e.g. 3AM on Spring DST day
            # goes back to 1AM on the previous day) - when this happens, advance to the correct
            # time that actually matches the cronstring
            next_date = date_iter.get_next(datetime.datetime)

        check.invariant(start_offset <= 0)
        for _ in range(-start_offset):
            next_date = date_iter.get_prev(datetime.datetime)

    if delta_fn is not None:
        # Use pendulums for intervals when possible
        next_date = to_timezone(pendulum.instance(next_date), timezone_str)
        while True:
            curr_hour = next_date.hour

            next_date_cand = delta_fn(next_date, 1)
            new_hour = next_date_cand.hour
            new_minute = next_date_cand.minute

            if not should_hour_change and new_hour != curr_hour:
                # If the hour changes during a daily/weekly/monthly schedule, it
                # indicates that the time shifted due to falling in a time that doesn't
                # exist due to a DST transition (for example, 2:30AM CST on 3/10/2019).
                # Instead, execute at the first time that does exist (the start of the hour),
                # but return to the original hour for all subsequent executions so that the
                # hour doesn't stay different permanently.

                check.invariant(new_hour == curr_hour + 1)
                yield next_date_cand.replace(minute=0)

                next_date_cand = delta_fn(next_date, 2)
                check.invariant(next_date_cand.hour == curr_hour)
            elif expected_hour is not None and new_hour != expected_hour:
                # hour should only be different than expected if the timezone has just changed -
                # if it hasn't, it means we are moving from e.g. 3AM on spring DST day back to
                # 2AM on the next day and need to reset back to the expected hour
                if next_date_cand.utcoffset() == next_date.utcoffset():
                    next_date_cand = next_date_cand.set(hour=expected_hour)

            if expected_minute is not None and new_minute != expected_minute:
                next_date_cand = next_date_cand.set(minute=expected_minute)

            next_date = next_date_cand

            if start_offset == 0 and next_date.timestamp() < start_timestamp:
                # Guard against edge cases where croniter get_prev() returns unexpected
                # results that cause us to get stuck
                continue

            yield next_date
    else:
        # Otherwise fall back to croniter
        check.invariant(
            not known_schedule_type,
            f"Should never need croniter on a {known_schedule_type} schedule",
        )

        while True:
            next_date = to_timezone(
                pendulum.instance(check.not_none(date_iter).get_next(datetime.datetime)),
                timezone_str,
            )

            if start_offset == 0 and next_date.timestamp() < start_timestamp:
                # Guard against edge cases where croniter get_prev() returns unexpected
                # results that cause us to get stuck
                continue

            yield next_date


def reverse_cron_string_iterator(
    end_timestamp: float, cron_string: str, execution_timezone: Optional[str]
) -> Iterator[datetime.datetime]:
    """Generator of datetimes < end_timestamp for the given cron string."""
    timezone_str = execution_timezone if execution_timezone else "UTC"

    utc_datetime = pytz.utc.localize(datetime.datetime.utcfromtimestamp(end_timestamp))
    end_datetime = utc_datetime.astimezone(pytz.timezone(timezone_str))

    date_iter = CroniterShim(cron_string, end_datetime)

    # Go forward one iteration so that the next iteration is the first time that is < end_datetime
    # and matches the cron schedule
    next_date = date_iter.get_next(datetime.datetime)

    # Croniter < 1.4 returns 2 items
    # Croniter >= 1.4 returns 3 items
    cron_parts, *_ = CroniterShim.expand(cron_string)

    is_numeric = [len(part) == 1 and part[0] != "*" for part in cron_parts]
    is_wildcard = [len(part) == 1 and part[0] == "*" for part in cron_parts]

    # Special-case common intervals (hourly/daily/weekly/monthly) since croniter iteration can be
    # much slower than adding a fixed interval
    if all(is_numeric[0:3]) and all(is_wildcard[3:]):  # monthly
        delta_fn = lambda d, num: d.subtract(months=num)
        should_hour_change = False
    elif all(is_numeric[0:2]) and is_numeric[4] and all(is_wildcard[2:4]):  # weekly
        delta_fn = lambda d, num: d.subtract(weeks=num)
        should_hour_change = False
    elif all(is_numeric[0:2]) and all(is_wildcard[2:]):  # daily
        delta_fn = lambda d, num: d.subtract(days=num)
        should_hour_change = False
    elif is_numeric[0] and all(is_wildcard[1:]):  # hourly
        delta_fn = lambda d, num: d.subtract(hours=num)
        should_hour_change = True
    else:
        delta_fn = None
        should_hour_change = False

    if delta_fn is not None:
        # Use pendulums for intervals when possible
        next_date = to_timezone(pendulum.instance(next_date), timezone_str)
        while True:
            curr_hour = next_date.hour

            next_date_cand = delta_fn(next_date, 1)
            new_hour = next_date_cand.hour

            if not should_hour_change and new_hour != curr_hour:
                # If the hour changes during a daily/weekly/monthly schedule, it
                # indicates that the time shifted due to falling in a time that doesn't
                # exist due to a DST transition (for example, 2:30AM CST on 3/10/2019).
                # Instead, execute at the first time that does exist (the start of the hour),
                # but return to the original hour for all subsequent executions so that the
                # hour doesn't stay different permanently.

                check.invariant(new_hour == curr_hour + 1)
                yield next_date_cand.replace(minute=0)

                next_date_cand = delta_fn(next_date, 2)
                check.invariant(next_date_cand.hour == curr_hour)

            next_date = next_date_cand

            if next_date.timestamp() > end_timestamp:
                # Guard against edge cases where croniter get_next() returns unexpected
                # results that cause us to get stuck
                continue

            yield next_date
    else:
        # Otherwise fall back to croniter
        while True:
            next_date = to_timezone(
                pendulum.instance(date_iter.get_prev(datetime.datetime)), timezone_str
            )

            if next_date.timestamp() > end_timestamp:
                # Guard against edge cases where croniter get_next() returns unexpected
                # results that cause us to get stuck
                continue

            yield next_date


def schedule_execution_time_iterator(
    start_timestamp: float,
    cron_schedule: Union[str, Sequence[str]],
    execution_timezone: Optional[str],
    ascending: bool = True,
) -> Iterator[datetime.datetime]:
    """Generator of execution datetimes >= start_timestamp for the given schedule.

    Here cron_schedule is either a cron string or a sequence of cron strings. In the latter case,
    the next execution datetime is obtained by computing the next cron datetime
    after the current execution datetime for each cron string in the sequence, and then choosing
    the earliest among them.
    """
    check.invariant(
        is_valid_cron_schedule(cron_schedule), desc=f"{cron_schedule} must be a valid cron schedule"
    )

    if isinstance(cron_schedule, str):
        yield from (
            cron_string_iterator(start_timestamp, cron_schedule, execution_timezone)
            if ascending
            else reverse_cron_string_iterator(start_timestamp, cron_schedule, execution_timezone)
        )
    else:
        iterators = [
            (
                cron_string_iterator(start_timestamp, cron_string, execution_timezone)
                if ascending
                else reverse_cron_string_iterator(start_timestamp, cron_string, execution_timezone)
            )
            for cron_string in cron_schedule
        ]
        next_dates = [next(it) for it in iterators]
        while True:
            # Choose earliest out of all subsequent datetimes.
            earliest_next_date = min(next_dates)
            yield earliest_next_date
            # Increment all iterators that generated the earliest subsequent datetime.
            for i, next_date in enumerate(next_dates):
                if next_date == earliest_next_date:
                    next_dates[i] = next(iterators[i])
