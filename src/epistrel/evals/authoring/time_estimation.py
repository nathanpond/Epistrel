"""E-09's labeled set (``evals/sets/time_estimation/cases.jsonl``; NFR-QUAL-9, FR-CLOCK-7; #201):
narrated time skips with their context and a human-judged acceptable duration range.

Each case is one sentence a render could narrate without declaring an advance, with what #199's
``time_estimation`` purpose receives for it (``TimeEstimationInputs``): ``narrated_text``, the
``activity_context`` the scene established, and the ``established_times`` the story stated
("The clock reads 19:40."). Its label is ``acceptable_minutes: [lo, hi]``, the range of story
minutes a reader would accept for what the sentence skips, from now.

Seven activity families (``FAMILIES``): meals, movies, drives, flights, sleep, waits and work
shifts, 28 or 29 cases each (200 in all). Each family cycles hand-written templates of three
kinds, named at the start of every case's ``notes``:

- ``explicit``: the sentence states the amount ("Twenty minutes later, …"); the range is the
  amount ± 10 % (at least 2 minutes);
- ``implicit``: no amount; the activity sets it ("They finish dinner and push back their
  chairs."), and the range is what such an activity ordinarily takes (hand-judged per
  template);
- ``inferred``: the established times give a start, a length or an end, and the sentence runs
  to it ("They watch until the credits roll." at 19:40 for a two-hour film that started at
  19:10: 90 minutes); the range is the remaining time ± a template's slack (5 % for an exact
  end such as an alarm or a timetable, 10 % for a typical length).

Templates draw names, places, titles, amounts and clock times from a seeded chooser; the
vocabulary is #199's ``ACTIVITY_LEXICON`` (watch, sleep, drive, travel, wait, work, cook, eat),
so a live turn would establish the same activities. ``stub_answer`` scripts the stub's
``time_estimation`` answer: the range's midpoint at confidence 0.9, so the stub run is in range
on every case. No model is used anywhere.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from epistrel.evals.authoring.common import Chooser

SET = "time_estimation"
FAMILIES: tuple[tuple[str, int], ...] = (
    ("meal", 29),
    ("movie", 29),
    ("drive", 29),
    ("flight", 29),
    ("sleep", 28),
    ("wait", 28),
    ("work-shift", 28),
)
"""Each activity family and its number of cases (200 in all)."""
STUB_CONFIDENCE = 0.9

NAMES = (
    "Bobby", "Ann", "Cal", "Jenni", "Maren", "Wren", "Raj", "Linda", "Marcus", "Elias",
    "Priya", "Owen", "Ruth", "Hugo", "Ada", "Felix", "Ines", "Tom",
)  # fmt: skip
TOWNS = ("Ashford", "Millbrook", "Harlow", "Kestrel Bay", "Dunmore", "Fairhaven", "Westwick")
AMOUNTS: dict[int, str] = {
    5: "five minutes",
    10: "ten minutes",
    15: "fifteen minutes",
    20: "twenty minutes",
    30: "half an hour",
    40: "forty minutes",
    45: "forty-five minutes",
    60: "an hour",
    90: "an hour and a half",
    120: "two hours",
    150: "two and a half hours",
    180: "three hours",
    240: "four hours",
    300: "five hours",
    360: "six hours",
    420: "seven hours",
    480: "eight hours",
    540: "nine hours",
    600: "ten hours",
    720: "twelve hours",
}
"""Story minutes and how a sentence states them."""
NUMBERS = {2: "two", 3: "three", 4: "four", 5: "five"}
HOURS = {4: "four", 6: "six", 8: "eight", 10: "ten", 12: "twelve"}

type Made = tuple[str, str, str, list[str], int, int, str]
"""A template's case: kind, narrated text, activity context, established times, lo, hi, note."""
type Template = Callable[[Chooser], Made]


def amount(minutes: int) -> str:
    return AMOUNTS[minutes]


def hm(minute: int) -> str:
    """A minute of the day (wrapping past midnight) as ``HH:MM``."""
    minute %= 1440
    return f"{minute // 60:02d}:{minute % 60:02d}"


def explicit_range(minutes: int) -> tuple[int, int]:
    """A stated amount ± 10 % (at least 2 minutes)."""
    slack = max(2, round(minutes * 0.1))
    return minutes - slack, minutes + slack


def remaining_range(minutes: int, share: float, least: int) -> tuple[int, int]:
    """The remaining time ± ``share`` of it (at least ``least`` minutes, never below 1)."""
    slack = max(least, round(minutes * share))
    return max(1, minutes - slack), minutes + slack


def _names(c: Chooser, k: int = 2) -> list[str]:
    return c.picks(NAMES, k)


def _explicit(
    text: str, context: str, minutes: int, note: str, times: list[str] | None = None
) -> Made:
    lo, hi = explicit_range(minutes)
    return ("explicit", text, context, times or [], lo, hi, note)


def _inferred(
    text: str, context: str, times: list[str], remaining: int, note: str, *, exact: bool
) -> Made:
    lo, hi = remaining_range(remaining, 0.05 if exact else 0.1, 3 if exact else 10)
    return ("inferred", text, context, times, lo, hi, note)


# -- meals --------------------------------------------------------------------------------------

MEALS = (("breakfast", 15, 60), ("lunch", 20, 90), ("dinner", 40, 150))
EATERIES = ("the diner", "the corner café", "the inn", "the noodle bar", "the kitchen table")
DISHES = (
    ("cooks", "a pot of beef stew", 90, 240),
    ("roasts", "a whole chicken", 75, 150),
    ("cooks", "a pan of scrambled eggs", 3, 15),
    ("cooks", "a bowl of pasta", 12, 40),
    ("bakes", "a loaf of bread from scratch", 120, 300),
    ("bakes", "a tray of biscuits", 20, 45),
)
OVEN = (("lasagne", (45, 60)), ("roast", (90, 120)), ("cake", (45, 60)), ("pie", (45, 60)))


def meal_explicit_plate(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    minutes = c.pick((15, 20, 30, 40, 45))
    meal = c.pick(MEALS)[0]
    text = f"{amount(minutes).capitalize()} later, {a} pushes the empty plate away."
    context = f"{a} sits down to {meal} at {c.pick(EATERIES)}."
    return _explicit(text, context, minutes, f"explicit: the sentence states {amount(minutes)}")


def meal_explicit_linger(c: Chooser) -> Made:
    a, b = _names(c)
    minutes = c.pick((60, 90, 120))
    meal = c.pick(("lunch", "dinner"))
    text = f"{a} and {b} linger over {meal} for {amount(minutes)}."
    context = f"{a} and {b} meet for {meal} at {c.pick(EATERIES)}."
    return _explicit(text, context, minutes, f"explicit: {amount(minutes)} over the meal")


def meal_explicit_cooking(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    minutes = c.pick((30, 45, 60, 90))
    dish = c.pick(("a curry", "a chicken soup", "a vegetable stew", "a risotto"))
    text = f"{a} spends {amount(minutes)} cooking {dish}, then serves it."
    return _explicit(
        text,
        f"{a} starts cooking in the kitchen.",
        minutes,
        f"explicit: {amount(minutes)} of cooking",
    )


def meal_implicit_finish(c: Chooser) -> Made:
    a, b = _names(c)
    meal, lo, hi = c.pick(MEALS)
    text = f"{a} and {b} finish {meal} and push back their chairs."
    context = f"{a} and {b} sit down to {meal} at {c.pick(EATERIES)}."
    return ("implicit", text, context, [], lo, hi, f"implicit: a sit-down {meal}")


def meal_implicit_sandwich(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    text = f"{a} eats a quick sandwich standing at the kitchen counter."
    context = f"{a} is hungry and has to leave again soon."
    return ("implicit", text, context, [], 3, 20, "implicit: a quick snack eaten standing")


def meal_implicit_dish(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    verb, dish, lo, hi = c.pick(DISHES)
    text = f"{a} {verb} {dish} and sets it on the table."
    context = f"{a} starts cooking in the kitchen."
    return ("implicit", text, context, [], lo, hi, f"implicit: how long {dish} takes to make")


def meal_inferred_dinner(c: Chooser) -> Made:
    a, b = _names(c)
    start = c.between(36, 41) * 30
    length = c.pick((90, 120))
    elapsed = c.between(4, 12) * 5
    place = c.pick(EATERIES[:4])
    times = [
        f"{a} and {b} sat down to dinner at {hm(start)}.",
        f"Dinner at {place} always runs about {amount(length)}.",
        f"The clock reads {hm(start + elapsed)}.",
    ]
    remaining = length - elapsed
    return _inferred(
        "They finish dinner and ask for the bill.",
        f"{a} and {b} are having dinner at {place}.",
        times,
        remaining,
        f"inferred: a {length}-minute dinner {elapsed} minutes in",
        exact=False,
    )


def meal_inferred_oven(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    dish, (low, high) = c.pick(OVEN)
    length = c.pick((low, high))
    start = c.between(34, 39) * 30
    elapsed = c.between(2, length // 5 - 2) * 5
    times = [
        f"{a} put the {dish} in the oven at {hm(start)}.",
        f"The {dish} needs {amount(length)} in the oven.",
        f"The clock reads {hm(start + elapsed)}.",
    ]
    return _inferred(
        f"{a} waits for the {dish} to finish baking and takes it out.",
        f"{a} is cooking dinner.",
        times,
        length - elapsed,
        f"inferred: {length} minutes in the oven, {elapsed} gone",
        exact=True,
    )


# -- movies -------------------------------------------------------------------------------------

FILMS = (
    "The Long Harbour", "Night Train South", "A Quiet Orchard", "The Glass Mile",
    "Paper Lanterns", "Winter at Dunmore", "The Last Ferry",
)  # fmt: skip
SHOWS = (
    ("a half-hour sitcom", 20, 35),
    ("an hour-long crime drama", 40, 65),
    ("a twenty-minute cartoon", 15, 25),
)


def movie_explicit_credits(c: Chooser) -> Made:
    a, b = _names(c)
    minutes = c.pick((90, 120, 150))
    film = c.pick(FILMS)
    text = f"{amount(minutes).capitalize()} later, the credits roll on {film}."
    context = f"{a} and {b} start watching {film} on the couch."
    return _explicit(text, context, minutes, f"explicit: the film ends {amount(minutes)} on")


def movie_explicit_episodes(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    count = c.pick((2, 3, 4))
    each = c.pick((20, 30, 45))
    if each == 30:
        text = f"{a} watches {NUMBERS[count]} half-hour episodes back to back."
    else:
        text = f"{a} watches {NUMBERS[count]} episodes back to back, {amount(each)} each."
    return _explicit(
        text,
        f"{a} settles in front of the television.",
        count * each,
        f"explicit: {count} episodes of {each} minutes",
    )


def movie_explicit_tv(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    minutes = c.pick((30, 60, 90, 120))
    text = f"{a} watches television for {amount(minutes)} before switching it off."
    return _explicit(
        text,
        f"{a} sits down in front of the television after work.",
        minutes,
        f"explicit: {amount(minutes)} of television",
    )


def movie_implicit_cinema(c: Chooser) -> Made:
    a, b = _names(c)
    film = c.pick(FILMS)
    text = "The film ends and the lights come up."
    context = f"{a} and {b} take their seats as {film} begins at the cinema."
    return ("implicit", text, context, [], 80, 160, "implicit: a feature film, start to end")


def movie_implicit_episode(c: Chooser) -> Made:
    a, b = _names(c)
    show, lo, hi = c.pick(SHOWS)
    text = f"{a} and {b} watch an episode together."
    context = f"{a} puts on {show}."
    return ("implicit", text, context, [], lo, hi, f"implicit: one episode of {show}")


def movie_implicit_match(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    text = f"{a} watches the whole football match on television."
    context = f"{a} switches on the television as the match kicks off."
    return ("implicit", text, context, [], 100, 130, "implicit: 90 minutes, half-time, stoppage")


def movie_implicit_double(c: Chooser) -> Made:
    a, b = _names(c)
    text = f"{a} and {b} sit through the whole double feature."
    context = "The cinema is showing two films back to back tonight."
    return ("implicit", text, context, [], 180, 300, "implicit: two feature films")


def movie_inferred_credits(c: Chooser) -> Made:
    a, b = _names(c)
    film = c.pick(FILMS)
    length = c.pick((90, 120, 150))
    start = c.between(114, 126) * 10
    elapsed = c.between(2, length // 10 - 2) * 10
    times = [
        f"The movie started at {hm(start)}.",
        f"It runs {amount(length)}.",
        f"The clock reads {hm(start + elapsed)}.",
    ]
    return _inferred(
        "They watch until the credits roll.",
        f"{a} and {b} are watching {film} on the couch.",
        times,
        length - elapsed,
        f"inferred: a {length}-minute film, {elapsed} minutes in",
        exact=True,
    )


def movie_inferred_match(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    start = c.between(36, 42) * 30
    elapsed = c.between(2, 8) * 10
    times = [f"The match kicked off at {hm(start)}.", f"The clock reads {hm(start + elapsed)}."]
    return _inferred(
        f"{a} watches until the final whistle.",
        f"{a} is watching the football match on television.",
        times,
        110 - elapsed,
        f"inferred: a match of about 110 minutes, {elapsed} in",
        exact=False,
    )


# -- drives -------------------------------------------------------------------------------------


def drive_explicit_coast(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    minutes = c.pick((20, 30, 45, 60, 90, 120))
    text = f"{a} drives for {amount(minutes)} along the coast road."
    return _explicit(
        text,
        f"{a} sets off from {c.pick(TOWNS)} in the car.",
        minutes,
        f"explicit: {amount(minutes)} of driving",
    )


def drive_explicit_arrive(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    town = c.pick(TOWNS)
    minutes = c.pick((40, 60, 90, 120, 180))
    text = f"After {amount(minutes)} on the motorway, {a} pulls into {town}."
    return _explicit(
        text,
        f"{a} is driving north to {town}.",
        minutes,
        f"explicit: {amount(minutes)} on the road",
    )


def drive_implicit_town(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    text = f"{a} drives across town to the hardware store."
    context = f"{a} gets into the car outside the house in {c.pick(TOWNS)}."
    return ("implicit", text, context, [], 10, 40, "implicit: a drive across a town")


def drive_implicit_miles(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    origin, town = c.picks(TOWNS, 2)
    miles = c.pick((30, 60, 100, 150, 200))
    text = f"{a} drives the {miles} miles to {town} on the highway."
    context = f"{a} gets on the highway at {origin}."
    lo, hi = round(miles * 0.8), round(miles * 1.4)
    return ("implicit", text, context, [], lo, hi, f"implicit: {miles} highway miles at 45-75 mph")


def drive_implicit_shop(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    text = f"{a} drives to the corner shop for milk and back."
    context = f"{a} grabs the car keys from the hook in the hallway."
    return ("implicit", text, context, [], 5, 20, "implicit: a short errand by car")


def drive_inferred_arrive(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    town = c.pick(TOWNS)
    length = c.pick((60, 90, 120, 180))
    start = c.between(16, 30) * 30
    elapsed = c.between(1, length // 15 - 1) * 15
    times = [
        f"{a} left home at {hm(start)}.",
        f"The drive to {town} takes about {amount(length)}.",
        f"The clock reads {hm(start + elapsed)}.",
    ]
    return _inferred(
        f"{a} drives on and pulls into {town}.",
        f"{a} is driving to {town}.",
        times,
        length - elapsed,
        f"inferred: a {length}-minute drive, {elapsed} minutes in",
        exact=False,
    )


# -- flights ------------------------------------------------------------------------------------

ROUTES = (
    ("London", "Paris", 60, 100),
    ("Paris", "Rome", 110, 150),
    ("New York", "Los Angeles", 300, 420),
    ("London", "New York", 420, 540),
    ("Sydney", "Melbourne", 70, 110),
    ("Chicago", "New York", 110, 170),
    ("Madrid", "Barcelona", 60, 100),
    ("Boston", "Washington", 70, 110),
    ("Frankfurt", "Singapore", 660, 780),
)
"""City pairs and the range of a scheduled flight between them, gate to gate."""
CITIES = ("Lisbon", "Dublin", "Vienna", "Oslo", "Athens", "Toronto", "Denver")


def flight_explicit_lands(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    city = c.pick(CITIES)
    minutes = c.pick((60, 90, 120, 180, 240, 300, 420))
    text = f"{amount(minutes).capitalize()} later, the plane lands in {city}."
    return _explicit(
        text, f"{a} boards the flight to {city}.", minutes, f"explicit: {amount(minutes)} aloft"
    )


def flight_explicit_takes(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    city = c.pick(CITIES)
    minutes = c.pick((90, 120, 180, 240, 360))
    text = f"The flight to {city} takes {amount(minutes)}, and {a} reads most of the way."
    return _explicit(
        text,
        f"{a} settles into a window seat on the flight to {city}.",
        minutes,
        f"explicit: a {minutes}-minute flight",
    )


def flight_implicit_route(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    origin, destination, lo, hi = c.pick(ROUTES)
    text = f"The flight from {origin} to {destination} lands."
    context = f"{a} boards the flight from {origin} to {destination}."
    return ("implicit", text, context, [], lo, hi, f"implicit: {origin} to {destination} by air")


def flight_inferred_due(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    city = c.pick(CITIES)
    length = c.pick((90, 120, 180, 240, 300))
    start = c.between(14, 40) * 30
    elapsed = c.between(1, length // 15 - 1) * 15
    times = [
        f"The plane took off at {hm(start)}.",
        f"It is due to land at {hm(start + length)}.",
        f"The clock reads {hm(start + elapsed)}.",
    ]
    return _inferred(
        "The plane lands on time.",
        f"{a} is on a flight to {city}.",
        times,
        length - elapsed,
        f"inferred: due at {hm(start + length)}, {length - elapsed} minutes away",
        exact=True,
    )


def flight_inferred_layover(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    city = c.pick(CITIES)
    now = c.between(16, 40) * 30
    wait = c.pick((45, 60, 90, 120, 150))
    times = [
        f"The connecting flight to {city} departs at {hm(now + wait)}.",
        f"The clock reads {hm(now)}.",
    ]
    return _inferred(
        f"{a} waits out the layover until the connecting flight departs.",
        f"{a} is travelling to {city} with a change of planes.",
        times,
        wait,
        f"inferred: the connection leaves in {wait} minutes",
        exact=True,
    )


# -- sleep --------------------------------------------------------------------------------------


def sleep_explicit_nap(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    minutes = c.pick((15, 20, 30, 45, 60, 90))
    text = f"{a} naps for {amount(minutes)} on the sofa."
    return _explicit(
        text,
        f"{a} lies down on the sofa after lunch.",
        minutes,
        f"explicit: a {minutes}-minute nap",
    )


def sleep_explicit_night(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    minutes = c.pick((360, 420, 480, 540, 600))
    text = f"{a} sleeps for {amount(minutes)} and wakes feeling better."
    return _explicit(text, f"{a} goes to bed.", minutes, f"explicit: {amount(minutes)} of sleep")


def sleep_implicit_night(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    text = f"{a} sleeps through the night."
    return ("implicit", text, f"{a} goes to bed.", [], 360, 600, "implicit: a night's sleep")


def sleep_implicit_doze(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    text = f"{a} dozes off in the armchair for a short nap."
    context = f"{a} sits down by the fire after lunch."
    return ("implicit", text, context, [], 10, 60, "implicit: a short nap")


def sleep_inferred_alarm(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    now = c.between(42, 49) * 30
    wake = c.between(10, 16) * 30
    remaining = (wake - now) % 1440
    times = [f"The clock reads {hm(now)}.", f"{a}'s alarm is set for {hm(wake)}."]
    return _inferred(
        f"{a} sleeps until the alarm goes off.",
        f"{a} goes to bed.",
        times,
        remaining,
        f"inferred: asleep from {hm(now)} to the {hm(wake)} alarm",
        exact=True,
    )


def sleep_inferred_dawn(c: Chooser) -> Made:
    a, b = _names(c)
    now = c.between(44, 47) * 30
    dawn = c.between(10, 14) * 30
    remaining = (dawn - now) % 1440
    times = [f"The clock reads {hm(now)}.", f"The sun rises at {hm(dawn)}."]
    return _inferred(
        f"{a} and {b} fall asleep on the couch and wake at dawn.",
        f"{a} and {b} are watching a film late at night, half asleep.",
        times,
        remaining,
        f"inferred: asleep from {hm(now)} to sunrise at {hm(dawn)}",
        exact=False,
    )


# -- waits --------------------------------------------------------------------------------------


def wait_explicit_bus(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    minutes = c.pick((10, 15, 20, 30, 40, 45, 60))
    text = f"{a} waits {amount(minutes)} for the bus."
    return _explicit(
        text,
        f"{a} reaches the bus stop in {c.pick(TOWNS)}.",
        minutes,
        f"explicit: {amount(minutes)} at the stop",
    )


def wait_explicit_counter(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    minutes = c.pick((10, 15, 20, 30))
    text = f"After waiting {amount(minutes)} at the counter, {a} is finally served."
    return _explicit(
        text,
        f"{a} joins the queue at the bank.",
        minutes,
        f"explicit: {amount(minutes)} waiting",
    )


def wait_implicit_kettle(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    text = f"{a} waits for the kettle to boil."
    context = f"{a} fills the kettle and switches it on."
    return ("implicit", text, context, [], 2, 6, "implicit: a kettle coming to the boil")


def wait_implicit_post(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    text = f"{a} waits in the queue at the post office until served."
    context = f"{a} joins a short queue at the post office in {c.pick(TOWNS)}."
    return ("implicit", text, context, [], 5, 30, "implicit: a short queue")


def wait_implicit_surgery(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    text = f"{a} waits at the doctor's surgery until called in."
    context = f"{a} checks in at reception for an appointment."
    return ("implicit", text, context, [], 5, 45, "implicit: a waiting room")


def wait_implicit_crossing(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    text = f"{a} waits at the level crossing while the freight train passes."
    context = f"{a} is walking home when the barrier comes down."
    return ("implicit", text, context, [], 2, 10, "implicit: a train passing a crossing")


def wait_inferred_train(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    town = c.pick(TOWNS)
    now = c.between(70, 130) * 10
    wait = c.pick((10, 15, 20, 25, 35, 50))
    times = [f"The next train to {town} is due at {hm(now + wait)}.", f"The clock reads {hm(now)}."]
    return _inferred(
        f"{a} waits on the platform until the train pulls in.",
        f"{a} is waiting for the train to {town}.",
        times,
        wait,
        f"inferred: the train is due in {wait} minutes",
        exact=True,
    )


def wait_inferred_shop(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    opens = c.pick((480, 510, 540, 600))
    wait = c.pick((10, 20, 30, 45, 60))
    times = [f"The shop opens at {hm(opens)}.", f"The clock reads {hm(opens - wait)}."]
    return _inferred(
        f"{a} waits outside until the shop opens.",
        f"{a} is waiting outside the shop in {c.pick(TOWNS)}.",
        times,
        wait,
        f"inferred: the shop opens in {wait} minutes",
        exact=True,
    )


# -- work shifts --------------------------------------------------------------------------------

WORKPLACES = ("the bakery", "the warehouse", "the hospital", "the call centre", "the docks")


def shift_explicit_hours(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    minutes = c.pick((120, 180, 240, 360, 480))
    place = c.pick(WORKPLACES)
    text = f"{a} works at {place} for {amount(minutes)} without a break."
    return _explicit(
        text, f"{a} starts work at {place}.", minutes, f"explicit: {amount(minutes)} of work"
    )


def shift_explicit_length(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    hours = c.pick((4, 6, 8, 10, 12))
    article = "an" if hours == 8 else "a"
    place = c.pick(WORKPLACES)
    text = f"{a} works {article} {HOURS[hours]}-hour shift at {place}."
    return _explicit(
        text,
        f"{a} arrives at {place} for work.",
        hours * 60,
        f"explicit: a {hours}-hour shift",
    )


def shift_implicit_night(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    text = f"{a} works the night shift at the hospital and goes home at dawn."
    context = f"{a} is a nurse on nights this week."
    return ("implicit", text, context, [], 480, 720, "implicit: a night shift")


def shift_implicit_double(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    place = c.pick(WORKPLACES)
    text = f"{a} works a double shift at {place}."
    context = f"{a} covers for a colleague at {place}."
    return ("implicit", text, context, [], 720, 1000, "implicit: two shifts back to back")


def shift_implicit_morning(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    place = c.pick(WORKPLACES)
    text = f"{a} finishes the morning shift at {place}."
    context = f"{a} starts the morning shift at {place}."
    return ("implicit", text, context, [], 240, 480, "implicit: a morning shift")


def shift_implicit_report(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    text = f"{a} works on the report until lunch."
    context = f"{a} sits down at the desk after breakfast to work on the quarterly report."
    return ("implicit", text, context, [], 120, 300, "implicit: a morning at the desk")


def shift_inferred_end(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    place = c.pick(WORKPLACES)
    length = c.pick((360, 480, 600, 720))
    start = c.between(12, 44) * 30
    elapsed = c.between(1, length // 30 - 1) * 30
    times = [
        f"{a}'s shift started at {hm(start)}.",
        f"Shifts at {place} are {amount(length)} long.",
        f"The clock reads {hm(start + elapsed)}.",
    ]
    return _inferred(
        f"{a} works until the end of the shift.",
        f"{a} is working at {place}.",
        times,
        length - elapsed,
        f"inferred: a {length // 60}-hour shift, {elapsed} minutes in",
        exact=True,
    )


def shift_inferred_closing(c: Chooser) -> Made:
    (a,) = _names(c, 1)
    place = c.pick(("the bar", "the restaurant", "the cinema", "the library"))
    closes = c.pick((1260, 1320, 1380))
    left = c.pick((45, 60, 90, 120, 180))
    times = [
        f"{place.capitalize()} closes at {hm(closes)}.",
        f"The clock reads {hm(closes - left)}.",
    ]
    return _inferred(
        f"{a} works until closing time.",
        f"{a} is working the late shift at {place}.",
        times,
        left,
        f"inferred: closing in {left} minutes",
        exact=True,
    )


TEMPLATES: dict[str, tuple[Template, ...]] = {
    "meal": (
        meal_explicit_plate,
        meal_implicit_finish,
        meal_inferred_dinner,
        meal_explicit_linger,
        meal_implicit_sandwich,
        meal_inferred_oven,
        meal_explicit_cooking,
        meal_implicit_dish,
    ),
    "movie": (
        movie_explicit_credits,
        movie_implicit_cinema,
        movie_inferred_credits,
        movie_explicit_episodes,
        movie_implicit_episode,
        movie_inferred_match,
        movie_explicit_tv,
        movie_implicit_match,
        movie_implicit_double,
    ),
    "drive": (
        drive_explicit_coast,
        drive_implicit_town,
        drive_inferred_arrive,
        drive_explicit_arrive,
        drive_implicit_miles,
        drive_implicit_shop,
    ),
    "flight": (
        flight_explicit_lands,
        flight_implicit_route,
        flight_inferred_due,
        flight_explicit_takes,
        flight_implicit_route,
        flight_inferred_layover,
    ),
    "sleep": (
        sleep_explicit_nap,
        sleep_implicit_night,
        sleep_inferred_alarm,
        sleep_explicit_night,
        sleep_implicit_doze,
        sleep_inferred_dawn,
    ),
    "wait": (
        wait_explicit_bus,
        wait_implicit_kettle,
        wait_inferred_train,
        wait_explicit_counter,
        wait_implicit_post,
        wait_inferred_shop,
        wait_implicit_surgery,
        wait_implicit_crossing,
    ),
    "work-shift": (
        shift_explicit_hours,
        shift_implicit_night,
        shift_inferred_end,
        shift_explicit_length,
        shift_implicit_double,
        shift_inferred_closing,
        shift_implicit_morning,
        shift_implicit_report,
    ),
}
"""Each family's templates, cycled in order (explicit, implicit and inferred interleaved)."""


def _case(family: str, n: int, made: Made) -> dict[str, Any]:
    kind, text, context, times, lo, hi, note = made
    assert 0 < lo <= hi, (family, n, made)  # noqa: S101 - an authoring invariant
    assert note.startswith(f"{kind}:"), note  # noqa: S101 - an authoring invariant
    midpoint = (lo + hi) // 2
    return {
        "id": f"{SET}-{family}-{n:04d}",
        "family": family,
        "narrated_text": text,
        "activity_context": context,
        "established_times": times,
        "acceptable_minutes": [lo, hi],
        "notes": note,
        "stub_answer": {
            "minutes": midpoint,
            "reason": note.split(": ", 1)[1],
            "confidence": STUB_CONFIDENCE,
        },
    }


def family_cases(family: str, count: int) -> list[dict[str, Any]]:
    """``count`` distinct cases of ``family``, its templates cycled from a seeded chooser."""
    c = Chooser(f"{SET}:{family}")
    templates = TEMPLATES[family]
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    attempt = 0
    while len(found) < count:
        if attempt > count * 50:
            raise RuntimeError(f"{family}: too few distinct cases")  # pragma: no cover
        made = templates[attempt % len(templates)](c)
        attempt += 1
        key = (made[1], made[2], tuple(made[3]))
        if key in seen:
            continue
        seen.add(key)
        found.append(_case(family, len(found) + 1, made))
    return found


def cases() -> list[dict[str, Any]]:
    """The 200 cases, family by family."""
    return [row for family, count in FAMILIES for row in family_cases(family, count)]


__all__ = ["FAMILIES", "SET", "TEMPLATES", "cases", "explicit_range", "remaining_range"]
