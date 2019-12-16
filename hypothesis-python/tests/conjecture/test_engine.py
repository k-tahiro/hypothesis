# coding=utf-8
#
# This file is part of Hypothesis, which may be found at
# https://github.com/HypothesisWorks/hypothesis/
#
# Most of this work is copyright (C) 2013-2019 David R. MacIver
# (david@drmaciver.com), but it contains contributions by others. See
# CONTRIBUTING.rst for a full list of people who may hold copyright, and
# consult the git log if you need to determine who owns an individual
# contribution.
#
# This Source Code Form is subject to the terms of the Mozilla Public License,
# v. 2.0. If a copy of the MPL was not distributed with this file, You can
# obtain one at https://mozilla.org/MPL/2.0/.
#
# END HEADER

from __future__ import absolute_import, division, print_function

import re
from random import Random

import pytest

import hypothesis.internal.conjecture.engine as engine_module
from hypothesis import HealthCheck, Phase, Verbosity, settings
from hypothesis.database import ExampleDatabase, InMemoryExampleDatabase
from hypothesis.errors import FailedHealthCheck, Flaky
from hypothesis.internal.compat import hbytes, hrange, int_from_bytes
from hypothesis.internal.conjecture.data import ConjectureData, Overrun, Status
from hypothesis.internal.conjecture.engine import (
    MIN_TEST_CALLS,
    ConjectureRunner,
    ExitReason,
)
from hypothesis.internal.conjecture.shrinker import Shrinker, block_program
from hypothesis.internal.conjecture.utils import integer_range
from hypothesis.internal.entropy import deterministic_PRNG
from tests.common.strategies import SLOW, HardToShrink
from tests.common.utils import no_shrink
from tests.conjecture.common import (
    SOME_LABEL,
    TEST_SETTINGS,
    buffer_size_limit,
    run_to_buffer,
    run_to_data,
    shrinking_from,
)


def test_can_index_results():
    @run_to_buffer
    def f(data):
        data.draw_bytes(5)
        data.mark_interesting()

    assert f.index(0) == 0
    assert f.count(0) == 5


def test_non_cloneable_intervals():
    @run_to_buffer
    def x(data):
        data.draw_bytes(10)
        data.draw_bytes(9)
        data.mark_interesting()

    assert x == hbytes(19)


def test_deletable_draws():
    @run_to_buffer
    def x(data):
        while True:
            x = data.draw_bytes(2)
            if x[0] == 255:
                data.mark_interesting()

    assert x == hbytes([255, 0])


def zero_dist(random, n):
    return hbytes(n)


def test_can_load_data_from_a_corpus():
    key = b"hi there"
    db = ExampleDatabase()
    value = b"=\xc3\xe4l\x81\xe1\xc2H\xc9\xfb\x1a\xb6bM\xa8\x7f"
    db.save(key, value)

    def f(data):
        if data.draw_bytes(len(value)) == value:
            data.mark_interesting()

    runner = ConjectureRunner(f, settings=settings(database=db), database_key=key)
    runner.run()
    (last_data,) = runner.interesting_examples.values()
    assert last_data.buffer == value
    assert len(list(db.fetch(key))) == 1


def slow_shrinker():
    strat = HardToShrink()

    def accept(data):
        if data.draw(strat):
            data.mark_interesting()

    return accept


@pytest.mark.parametrize("n", [1, 5])
def test_terminates_shrinks(n, monkeypatch):
    from hypothesis.internal.conjecture import engine

    db = InMemoryExampleDatabase()

    def generate_new_examples(self):
        self.cached_test_function([255] * 1000)

    monkeypatch.setattr(
        ConjectureRunner, "generate_new_examples", generate_new_examples
    )
    monkeypatch.setattr(engine, "MAX_SHRINKS", n)

    runner = ConjectureRunner(
        slow_shrinker(),
        settings=settings(max_examples=5000, database=db),
        random=Random(0),
        database_key=b"key",
    )
    runner.run()
    (last_data,) = runner.interesting_examples.values()
    assert last_data.status == Status.INTERESTING
    assert runner.shrinks == n
    in_db = set(db.data[runner.secondary_key])
    assert len(in_db) == n


def test_detects_flakiness():
    failed_once = [False]
    count = [0]

    def tf(data):
        data.draw_bytes(1)
        count[0] += 1
        if not failed_once[0]:
            failed_once[0] = True
            data.mark_interesting()

    runner = ConjectureRunner(tf)
    runner.run()
    assert runner.exit_reason == ExitReason.flaky
    assert count == [MIN_TEST_CALLS + 1]


def test_variadic_draw():
    def draw_list(data):
        result = []
        while True:
            data.start_example(SOME_LABEL)
            d = data.draw_bytes(1)[0] & 7
            if d:
                result.append(data.draw_bytes(d))
            data.stop_example()
            if not d:
                break
        return result

    @run_to_buffer
    def b(data):
        if any(all(d) for d in draw_list(data)):
            data.mark_interesting()

    ls = draw_list(ConjectureData.for_buffer(b))
    assert len(ls) == 1
    assert len(ls[0]) == 1


def test_draw_to_overrun():
    @run_to_buffer
    def x(data):
        d = (data.draw_bytes(1)[0] - 8) & 0xFF
        data.draw_bytes(128 * d)
        if d >= 2:
            data.mark_interesting()

    assert x == hbytes([10]) + hbytes(128 * 2)


def test_can_navigate_to_a_valid_example():
    def f(data):
        i = int_from_bytes(data.draw_bytes(2))
        data.draw_bytes(i)
        data.mark_interesting()

    runner = ConjectureRunner(f, settings=settings(max_examples=5000, database=None))
    with buffer_size_limit(2):
        runner.run()
    assert runner.interesting_examples


def test_stops_after_max_examples_when_reading():
    key = b"key"

    db = ExampleDatabase(":memory:")
    for i in range(10):
        db.save(key, hbytes([i]))

    seen = []

    def f(data):
        seen.append(data.draw_bytes(1))

    runner = ConjectureRunner(
        f, settings=settings(max_examples=1, database=db), database_key=key
    )
    runner.run()
    assert len(seen) == 1


def test_stops_after_max_examples_when_generating():
    seen = []

    def f(data):
        seen.append(data.draw_bytes(1))

    runner = ConjectureRunner(f, settings=settings(max_examples=1, database=None))
    runner.run()
    assert len(seen) == 1


@pytest.mark.parametrize("examples", [1, 5, 20, 50])
def test_stops_after_max_examples_when_generating_more_bugs(examples):
    seen = []
    bad = [False, False]

    def f(data):
        seen.append(data.draw_bits(32))
        # Rare, potentially multi-error conditions
        if seen[-1] > 2 ** 31:
            bad[0] = True
            raise ValueError
        bad[1] = True
        raise Exception

    runner = ConjectureRunner(
        f, settings=settings(max_examples=examples, phases=[Phase.generate])
    )
    try:
        runner.run()
    except Exception:
        pass
    # No matter what, whether examples is larger or smalller than MAX_TEST_CALLS,
    # we stop looking at max_examples.  (and re-run each failure for the traceback)
    assert len(seen) <= examples + sum(bad)


def test_interleaving_engines():
    children = []

    @run_to_buffer
    def x(data):
        rnd = Random(data.draw_bytes(1))

        def g(d2):
            d2.draw_bytes(1)
            data.mark_interesting()

        runner = ConjectureRunner(g, random=rnd)
        children.append(runner)
        runner.run()
        if runner.interesting_examples:
            data.mark_interesting()

    assert x == b"\0"
    for c in children:
        assert not c.interesting_examples


def test_phases_can_disable_shrinking():
    seen = set()

    def f(data):
        seen.add(hbytes(data.draw_bytes(32)))
        data.mark_interesting()

    runner = ConjectureRunner(
        f, settings=settings(database=None, phases=(Phase.reuse, Phase.generate))
    )
    runner.run()
    assert len(seen) == MIN_TEST_CALLS


def test_erratic_draws():
    n = [0]

    with pytest.raises(Flaky):

        @run_to_buffer
        def x(data):
            data.draw_bytes(n[0])
            data.draw_bytes(255 - n[0])
            if n[0] == 255:
                data.mark_interesting()
            else:
                n[0] += 1


def test_no_read_no_shrink():
    count = [0]

    @run_to_buffer
    def x(data):
        count[0] += 1
        data.mark_interesting()

    assert x == b""
    assert count == [1]


def test_one_dead_branch():
    with deterministic_PRNG():
        seen = set()

        @run_to_buffer
        def x(data):
            i = data.draw_bytes(1)[0]
            if i > 0:
                data.mark_invalid()
            i = data.draw_bytes(1)[0]
            if len(seen) < 255:
                seen.add(i)
            elif i not in seen:
                data.mark_interesting()


def test_does_not_save_on_interrupt():
    def interrupts(data):
        raise KeyboardInterrupt()

    db = InMemoryExampleDatabase()

    runner = ConjectureRunner(
        interrupts, settings=settings(database=db), database_key=b"key"
    )

    with pytest.raises(KeyboardInterrupt):
        runner.run()
    assert not db.data


def test_returns_written():
    value = hbytes(b"\0\1\2\3")

    @run_to_buffer
    def written(data):
        data.write(value)
        data.mark_interesting()

    assert value == written


def fails_health_check(label, **kwargs):
    def accept(f):
        runner = ConjectureRunner(
            f,
            settings=settings(
                max_examples=100, phases=no_shrink, database=None, **kwargs
            ),
        )

        with pytest.raises(FailedHealthCheck) as e:
            runner.run()
        assert e.value.health_check == label
        assert not runner.interesting_examples

    return accept


def test_fails_health_check_for_all_invalid():
    @fails_health_check(HealthCheck.filter_too_much)
    def _(data):
        data.draw_bytes(2)
        data.mark_invalid()


def test_fails_health_check_for_large_base():
    @fails_health_check(HealthCheck.large_base_example)
    def _(data):
        data.draw_bytes(10 ** 6)


def test_fails_health_check_for_large_non_base():
    @fails_health_check(HealthCheck.data_too_large)
    def _(data):
        if data.draw_bits(8):
            data.draw_bytes(10 ** 6)


def test_fails_health_check_for_slow_draws():
    @fails_health_check(HealthCheck.too_slow)
    def _(data):
        data.draw(SLOW)


@pytest.mark.parametrize("n_large", [1, 5, 8, 15])
def test_can_shrink_variable_draws(n_large):
    target = 128 * n_large

    @run_to_data
    def data(data):
        n = data.draw_bits(4)
        b = [data.draw_bits(8) for _ in hrange(n)]
        if sum(b) >= target:
            data.mark_interesting()

    x = data.buffer

    assert x.count(0) == 0
    assert sum(x[1:]) == target


def test_run_nothing():
    def f(data):
        assert False

    runner = ConjectureRunner(f, settings=settings(phases=()))
    runner.run()
    assert runner.call_count == 0


class Foo(object):
    def __repr__(self):
        return "stuff"


@pytest.mark.parametrize("event", ["hi", Foo()])
def test_note_events(event):
    def f(data):
        data.note_event(event)
        data.draw_bytes(1)

    runner = ConjectureRunner(f)
    runner.run()
    assert runner.event_call_counts[str(event)] == runner.call_count > 0


def test_debug_data(capsys):
    buf = [0, 1, 2]

    def f(data):
        for x in hbytes(buf):
            if data.draw_bits(8) != x:
                data.mark_invalid()
            data.start_example(1)
            data.stop_example()
        data.mark_interesting()

    runner = ConjectureRunner(
        f,
        settings=settings(
            max_examples=5000,
            database=None,
            suppress_health_check=HealthCheck.all(),
            verbosity=Verbosity.debug,
        ),
    )
    runner.cached_test_function(buf)
    runner.run()

    out, _ = capsys.readouterr()
    assert re.match(u"\\d+ bytes \\[.*\\] -> ", out)
    assert "INTERESTING" in out


def test_can_write_bytes_towards_the_end():
    buf = b"\1\2\3"

    def f(data):
        if data.draw_bits(1):
            data.draw_bytes(5)
            data.write(hbytes(buf))
            assert hbytes(data.buffer[-len(buf) :]) == buf

    with buffer_size_limit(10):
        ConjectureRunner(f).run()


def test_uniqueness_is_preserved_when_writing_at_beginning():
    seen = set()

    def f(data):
        data.write(hbytes(1))
        n = data.draw_bits(3)
        assert n not in seen
        seen.add(n)

    runner = ConjectureRunner(f, settings=settings(max_examples=50))
    runner.run()
    assert runner.valid_examples == len(seen)


@pytest.mark.parametrize("skip_target", [False, True])
@pytest.mark.parametrize("initial_attempt", [127, 128])
def test_clears_out_its_database_on_shrinking(
    initial_attempt, skip_target, monkeypatch
):
    def generate_new_examples(self):
        self.cached_test_function(initial_attempt)

    monkeypatch.setattr(
        ConjectureRunner, "generate_new_examples", generate_new_examples
    )

    key = b"key"
    db = InMemoryExampleDatabase()

    def f(data):
        if data.draw_bits(8) >= 127:
            data.mark_interesting()

    runner = ConjectureRunner(
        f,
        settings=settings(database=db, max_examples=256),
        database_key=key,
        random=Random(0),
    )

    for n in hrange(256):
        if n != 127 or not skip_target:
            db.save(runner.secondary_key, hbytes([n]))
    runner.run()
    assert len(runner.interesting_examples) == 1
    for b in db.fetch(runner.secondary_key):
        assert b[0] >= 127
    assert len(list(db.fetch(runner.database_key))) == 1


def test_detects_too_small_block_starts():
    call_count = [0]

    def f(data):
        assert call_count[0] == 0
        call_count[0] += 1
        data.draw_bytes(8)
        data.mark_interesting()

    runner = ConjectureRunner(f, settings=settings(database=None))
    r = runner.cached_test_function(hbytes(8))
    assert r.status == Status.INTERESTING
    assert call_count[0] == 1
    r2 = runner.cached_test_function(hbytes([255] * 7))
    assert r2.status == Status.OVERRUN
    assert call_count[0] == 1


def test_shrinks_both_interesting_examples(monkeypatch):
    def generate_new_examples(self):
        self.cached_test_function(hbytes([1]))

    monkeypatch.setattr(
        ConjectureRunner, "generate_new_examples", generate_new_examples
    )

    def f(data):
        n = data.draw_bits(8)
        data.mark_interesting(n & 1)

    runner = ConjectureRunner(f, database_key=b"key")
    runner.run()
    assert runner.interesting_examples[0].buffer == hbytes([0])
    assert runner.interesting_examples[1].buffer == hbytes([1])


def test_discarding(monkeypatch):
    monkeypatch.setattr(Shrinker, "shrink", Shrinker.remove_discarded)
    monkeypatch.setattr(
        ConjectureRunner,
        "generate_new_examples",
        lambda runner: runner.cached_test_function(hbytes([0, 1] * 10)),
    )

    @run_to_buffer
    def x(data):
        count = 0
        while count < 10:
            data.start_example(SOME_LABEL)
            b = data.draw_bits(1)
            if b:
                count += 1
            data.stop_example(discard=not b)
        data.mark_interesting()

    assert x == hbytes(hbytes([1]) * 10)


def test_can_remove_discarded_data():
    @shrinking_from(hbytes([0] * 10) + hbytes([11]))
    def shrinker(data):
        while True:
            data.start_example(SOME_LABEL)
            b = data.draw_bits(8)
            data.stop_example(discard=(b == 0))
            if b == 11:
                break
        data.mark_interesting()

    shrinker.remove_discarded()
    assert list(shrinker.buffer) == [11]


def test_discarding_iterates_to_fixed_point():
    @shrinking_from(hbytes(list(hrange(100, -1, -1))))
    def shrinker(data):
        data.start_example(0)
        data.draw_bits(8)
        data.stop_example(discard=True)
        while data.draw_bits(8):
            pass
        data.mark_interesting()

    shrinker.remove_discarded()
    assert list(shrinker.buffer) == [1, 0]


def test_discarding_is_not_fooled_by_empty_discards():
    @shrinking_from(hbytes([1, 1]))
    def shrinker(data):
        data.draw_bits(1)
        data.start_example(0)
        data.stop_example(discard=True)
        data.draw_bits(1)
        data.mark_interesting()

    shrinker.remove_discarded()
    assert shrinker.shrink_target.has_discards


def test_discarding_can_fail(monkeypatch):
    @shrinking_from(hbytes([1]))
    def shrinker(data):
        data.start_example(0)
        data.draw_bits(1)
        data.stop_example(discard=True)
        data.mark_interesting()

    shrinker.remove_discarded()
    assert any(e.discarded and e.length > 0 for e in shrinker.shrink_target.examples)


def test_shrinking_from_mostly_zero(monkeypatch):
    monkeypatch.setattr(
        ConjectureRunner,
        "generate_new_examples",
        lambda self: self.cached_test_function(hbytes(5) + hbytes([2])),
    )

    @run_to_buffer
    def x(data):
        s = [data.draw_bits(8) for _ in hrange(6)]
        if any(s):
            data.mark_interesting()

    assert x == hbytes(5) + hbytes([1])


def test_handles_nesting_of_discard_correctly(monkeypatch):
    monkeypatch.setattr(Shrinker, "shrink", Shrinker.remove_discarded)
    monkeypatch.setattr(
        ConjectureRunner,
        "generate_new_examples",
        lambda runner: runner.cached_test_function(hbytes([0, 0, 1, 1])),
    )

    @run_to_buffer
    def x(data):
        while True:
            data.start_example(SOME_LABEL)
            succeeded = data.draw_bits(1)
            data.start_example(SOME_LABEL)
            data.draw_bits(1)
            data.stop_example(discard=not succeeded)
            data.stop_example(discard=not succeeded)
            if succeeded:
                data.mark_interesting()

    assert x == hbytes([1, 1])


def test_database_clears_secondary_key():
    key = b"key"
    database = InMemoryExampleDatabase()

    def f(data):
        if data.draw_bits(8) == 10:
            data.mark_interesting()
        else:
            data.mark_invalid()

    runner = ConjectureRunner(
        f,
        settings=settings(
            max_examples=1, database=database, suppress_health_check=HealthCheck.all()
        ),
        database_key=key,
    )

    for i in range(10):
        database.save(runner.secondary_key, hbytes([i]))

    runner.cached_test_function([10])
    assert runner.interesting_examples

    assert len(set(database.fetch(key))) == 1
    assert len(set(database.fetch(runner.secondary_key))) == 10

    runner.clear_secondary_key()

    assert len(set(database.fetch(key))) == 1
    assert len(set(database.fetch(runner.secondary_key))) == 0


def test_database_uses_values_from_secondary_key():
    key = b"key"
    database = InMemoryExampleDatabase()

    def f(data):
        if data.draw_bits(8) >= 5:
            data.mark_interesting()
        else:
            data.mark_invalid()

    runner = ConjectureRunner(
        f,
        settings=settings(
            max_examples=1, database=database, suppress_health_check=HealthCheck.all()
        ),
        database_key=key,
    )

    for i in range(10):
        database.save(runner.secondary_key, hbytes([i]))

    runner.cached_test_function([10])
    assert runner.interesting_examples

    assert len(set(database.fetch(key))) == 1
    assert len(set(database.fetch(runner.secondary_key))) == 10

    runner.clear_secondary_key()

    assert len(set(database.fetch(key))) == 1
    assert set(map(int_from_bytes, database.fetch(runner.secondary_key))) == set(
        range(6, 11)
    )

    (v,) = runner.interesting_examples.values()

    assert list(v.buffer) == [5]


def test_exit_because_max_iterations():
    def f(data):
        data.draw_bits(64)
        data.mark_invalid()

    runner = ConjectureRunner(
        f,
        settings=settings(
            max_examples=1, database=None, suppress_health_check=HealthCheck.all()
        ),
    )

    runner.run()

    assert runner.call_count <= 1000
    assert runner.exit_reason == ExitReason.max_iterations


def test_dependent_block_pairs_can_lower_to_zero():
    @shrinking_from([1, 0, 1])
    def shrinker(data):
        if data.draw_bits(1):
            n = data.draw_bits(16)
        else:
            n = data.draw_bits(8)

        if n == 1:
            data.mark_interesting()

    shrinker.fixate_shrink_passes(["minimize_individual_blocks"])
    assert list(shrinker.shrink_target.buffer) == [0, 1]


def test_handle_size_too_large_during_dependent_lowering():
    @shrinking_from([1, 255, 0])
    def shrinker(data):
        if data.draw_bits(1):
            data.draw_bits(16)
            data.mark_interesting()
        else:
            data.draw_bits(8)

    shrinker.fixate_shrink_passes(["minimize_individual_blocks"])


def test_zero_examples_will_zero_blocks():
    @shrinking_from([1, 1, 1])
    def shrinker(data):
        n = data.draw_bits(1)
        data.draw_bits(1)
        m = data.draw_bits(1)
        if n == m == 1:
            data.mark_interesting()

    shrinker.fixate_shrink_passes(["zero_examples"])
    assert list(shrinker.shrink_target.buffer) == [1, 0, 1]


def test_block_may_grow_during_lexical_shrinking():
    initial = hbytes([2, 1, 1])

    @shrinking_from(initial)
    def shrinker(data):
        n = data.draw_bits(8)
        if n == 2:
            data.draw_bits(8)
            data.draw_bits(8)
        else:
            data.draw_bits(16)
        data.mark_interesting()

    shrinker.fixate_shrink_passes(["minimize_individual_blocks"])
    assert list(shrinker.shrink_target.buffer) == [0, 0, 0]


def test_lower_common_block_offset_does_nothing_when_changed_blocks_are_zero():
    @shrinking_from([1, 0, 1, 0])
    def shrinker(data):
        data.draw_bits(1)
        data.draw_bits(1)
        data.draw_bits(1)
        data.draw_bits(1)
        data.mark_interesting()

    shrinker.mark_changed(1)
    shrinker.mark_changed(3)
    shrinker.lower_common_block_offset()
    assert list(shrinker.shrink_target.buffer) == [1, 0, 1, 0]


def test_lower_common_block_offset_ignores_zeros():
    @shrinking_from([2, 2, 0])
    def shrinker(data):
        n = data.draw_bits(8)
        data.draw_bits(8)
        data.draw_bits(8)
        if n > 0:
            data.mark_interesting()

    for i in range(3):
        shrinker.mark_changed(i)
    shrinker.lower_common_block_offset()
    assert list(shrinker.shrink_target.buffer) == [1, 1, 0]


def test_pandas_hack():
    @shrinking_from([2, 1, 1, 7])
    def shrinker(data):
        n = data.draw_bits(8)
        m = data.draw_bits(8)
        if n == 1:
            if m == 7:
                data.mark_interesting()
        data.draw_bits(8)
        if data.draw_bits(8) == 7:
            data.mark_interesting()

    shrinker.fixate_shrink_passes([block_program("-XX")])
    assert list(shrinker.shrink_target.buffer) == [1, 7]


def test_cached_test_function_returns_right_value():
    count = [0]

    def tf(data):
        count[0] += 1
        data.draw_bits(2)
        data.mark_interesting()

    with deterministic_PRNG():
        runner = ConjectureRunner(tf, settings=TEST_SETTINGS)
        for _ in hrange(2):
            for b in (b"\0", b"\1"):
                d = runner.cached_test_function(b)
                assert d.status == Status.INTERESTING
                assert d.buffer == b
        assert count[0] == 2


def test_cached_test_function_does_not_reinvoke_on_prefix():
    call_count = [0]

    def test_function(data):
        call_count[0] += 1
        data.draw_bits(8)
        data.write(hbytes([7]))
        data.draw_bits(8)

    with deterministic_PRNG():
        runner = ConjectureRunner(test_function, settings=TEST_SETTINGS)

        data = runner.cached_test_function(hbytes(3))
        assert data.status == Status.VALID
        for n in [2, 1, 0]:
            prefix_data = runner.cached_test_function(hbytes(n))
            assert prefix_data is Overrun
        assert call_count[0] == 1


def test_will_evict_entries_from_the_cache(monkeypatch):
    monkeypatch.setattr(engine_module, "CACHE_SIZE", 5)
    count = [0]

    def tf(data):
        data.draw_bytes(1)
        count[0] += 1

    runner = ConjectureRunner(tf, settings=TEST_SETTINGS)

    for _ in range(3):
        for n in range(10):
            runner.cached_test_function([n])

    # Because we exceeded the cache size, our previous
    # calls will have been evicted, so each call to
    # cached_test_function will have to reexecute.
    assert count[0] == 30


def test_branch_ending_in_write():
    seen = set()

    def tf(data):
        count = 0
        while data.draw_bits(1):
            count += 1

        if count > 1:
            data.draw_bits(1, forced=0)

        b = hbytes(data.buffer)
        assert b not in seen
        seen.add(b)

    with deterministic_PRNG():
        runner = ConjectureRunner(tf, settings=TEST_SETTINGS)

        for _ in hrange(100):
            prefix = runner.generate_novel_prefix()
            attempt = prefix + hbytes(2)
            data = runner.cached_test_function(attempt)
            assert data.status == Status.VALID
            assert attempt.startswith(data.buffer)


def test_exhaust_space():
    with deterministic_PRNG():
        runner = ConjectureRunner(
            lambda data: data.draw_bits(1), settings=TEST_SETTINGS
        )
        runner.run()
        assert runner.tree.is_exhausted
        assert runner.valid_examples == 2


SMALL_COUNT_SETTINGS = settings(TEST_SETTINGS, max_examples=500)


def test_discards_kill_branches():
    starts = set()

    with deterministic_PRNG():

        def test(data):
            assert runner.call_count <= 256
            while True:
                data.start_example(1)
                b = data.draw_bits(8)
                data.stop_example(b != 0)
                if len(data.buffer) == 1:
                    s = hbytes(data.buffer)
                    assert s not in starts
                    starts.add(s)
                if b == 0:
                    break

        runner = ConjectureRunner(test, settings=SMALL_COUNT_SETTINGS)
        runner.run()
        assert runner.call_count == 256


@pytest.mark.parametrize("n", range(1, 32))
def test_number_of_examples_in_integer_range_is_bounded(n):
    with deterministic_PRNG():

        def test(data):
            assert runner.call_count <= 2 * n
            integer_range(data, 0, n)

        runner = ConjectureRunner(test, settings=SMALL_COUNT_SETTINGS)
        runner.run()


def test_prefix_cannot_exceed_buffer_size(monkeypatch):
    buffer_size = 10
    monkeypatch.setattr(engine_module, "BUFFER_SIZE", buffer_size)

    with deterministic_PRNG():

        def test(data):
            while data.draw_bits(1):
                assert len(data.buffer) <= buffer_size
            assert len(data.buffer) <= buffer_size

        runner = ConjectureRunner(test, settings=SMALL_COUNT_SETTINGS)
        runner.run()
        assert runner.valid_examples == buffer_size


def test_does_not_shrink_multiple_bugs_when_told_not_to():
    def test(data):
        m = data.draw_bits(8)
        n = data.draw_bits(8)

        if m > 0:
            data.mark_interesting(1)
        if n > 5:
            data.mark_interesting(2)

    with deterministic_PRNG():
        runner = ConjectureRunner(
            test, settings=settings(TEST_SETTINGS, report_multiple_bugs=False)
        )
        runner.cached_test_function([255, 255])
        runner.shrink_interesting_examples()

        results = {d.buffer for d in runner.interesting_examples.values()}

    assert len(results.intersection({hbytes([0, 1]), hbytes([1, 0])})) == 1


def test_does_not_keep_generating_when_multiple_bugs():
    def test(data):
        if data.draw_bits(64) > 0:
            data.draw_bits(64)
            data.mark_interesting()

    with deterministic_PRNG():
        runner = ConjectureRunner(
            test,
            settings=settings(
                TEST_SETTINGS, report_multiple_bugs=False, phases=[Phase.generate]
            ),
        )

        runner.run()

    assert runner.call_count == 2