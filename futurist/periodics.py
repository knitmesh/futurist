# -*- coding: utf-8 -*-

#    Copyright (C) 2015 Yahoo! Inc. All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import fractions
import functools
import heapq
import inspect
import logging
import threading
import traceback

# For: https://wiki.openstack.org/wiki/Security/Projects/Bandit
from random import SystemRandom as random

import six

import futurist
from futurist import _utils as utils

LOG = logging.getLogger(__name__)

_REQUIRED_ATTRS = ('_is_periodic', '_periodic_spacing',
                   '_periodic_run_immediately')


def _check_attrs(obj):
    """Checks that a periodic function/method has all the expected attributes.

    This will return the expected attributes that were **not** found.
    """
    missing_attrs = []
    for attr_name in _REQUIRED_ATTRS:
        if not hasattr(obj, attr_name):
            missing_attrs.append(attr_name)
    return missing_attrs


def periodic(spacing, run_immediately=False):
    """Tags a method/function as wanting/able to execute periodically.

    :param spacing: how often to run the decorated function (required)
    :type spacing: float/int
    :param run_immediately: option to specify whether to run
                            immediately or wait until the spacing provided has
                            elapsed before running for the first time
    :type run_immediately: boolean
    """

    if spacing <= 0:
        raise ValueError("Periodicity/spacing must be greater than"
                         " zero instead of %s" % spacing)

    def wrapper(f):
        f._is_periodic = True
        f._periodic_spacing = spacing
        f._periodic_run_immediately = run_immediately

        @six.wraps(f)
        def decorator(*args, **kwargs):
            return f(*args, **kwargs)

        return decorator

    return wrapper


def _add_jitter(max_percent_jitter):
    """Wraps a existing strategy and adds jitter to it.

    0% to 100% of the spacing value will be added to this value to ensure
    callbacks do not synchronize.
    """
    if max_percent_jitter > 1 or max_percent_jitter < 0:
        raise ValueError("Invalid 'max_percent_jitter', must be greater or"
                         " equal to 0.0 and less than or equal to 1.0")

    def wrapper(func):

        @six.wraps(func)
        def decorator(cb, metrics, now=None):
            next_run = func(cb, metrics, now=now)
            how_often = cb._periodic_spacing
            jitter = how_often * (random.random() * max_percent_jitter)
            return next_run + jitter

        decorator.__name__ += "_with_jitter"
        return decorator

    return wrapper


def _last_finished_strategy(cb, started_at, finished_at, metrics):
    # Determine when the callback should next run based on when it was
    # last finished **only** given metrics about this information.
    how_often = cb._periodic_spacing
    return finished_at + how_often


def _last_started_strategy(cb, started_at, finished_at, metrics):
    # Determine when the callback should next run based on when it was
    # last started **only** given metrics about this information.
    how_often = cb._periodic_spacing
    return started_at + how_often


def _now_plus_periodicity(cb, now):
    how_often = cb._periodic_spacing
    return how_often + now


class _Schedule(object):
    """Internal heap-based structure that maintains the schedule/ordering.

    This stores a heap composed of the following (next_run, index) where
    next_run is the next desired runtime for the callback that is stored
    somewhere with the index provided. The index is saved so that if two
    functions with the same next_run time are inserted, that the one with
    the smaller index is preferred (it is also saved so that on pop we can
    know what the index of the callback we should call is).
    """

    def __init__(self):
        self._ordering = []

    def push(self, next_run, index):
        heapq.heappush(self._ordering, (next_run, index))

    def __len__(self):
        return len(self._ordering)

    def pop(self):
        return heapq.heappop(self._ordering)


def _run_callback(cb, *args, **kwargs):
    # NOTE(harlowja): this needs to be a module level function so that the
    # process pool execution can locate it (it can't be a lambda or method
    # local function because it won't be able to find those).
    pretty_tb = None
    started_at = utils.now()
    try:
        cb(*args, **kwargs)
    except Exception:
        # Until https://bugs.python.org/issue24451 is merged we have to
        # capture and return the traceback, so that we can have reliable
        # timing information.
        pretty_tb = traceback.format_exc()
    finished_at = utils.now()
    return (started_at, finished_at, pretty_tb)


def _build(callables, next_run_scheduler):
    schedule = _Schedule()
    now = None
    immediates = []
    # Reverse order is used since these are later popped off (and to
    # ensure the popping order is first -> last we need to append them
    # in the opposite ordering last -> first).
    reverse_it = utils.reverse_enumerate(callables)
    for index, (cb, _cb_name, args, kwargs) in reverse_it:
        if cb._periodic_run_immediately:
            immediates.append(index)
        else:
            if now is None:
                now = utils.now()
            next_run = next_run_scheduler(cb, now)
            schedule.push(next_run, index)
    return immediates, schedule


class PeriodicWorker(object):
    """Calls a collection of callables periodically (sleeping as needed...).

    NOTE(harlowja): typically the :py:meth:`.start` method is executed in a
    background thread so that the periodic callables are executed in
    the background/asynchronously (using the defined periods to determine
    when each is called).
    """

    #: Max amount of time to wait when running (forces a wakeup when elapsed).
    MAX_LOOP_IDLE = 30

    _NO_OP_ARGS = ()
    _NO_OP_KWARGS = {}
    _INITIAL_METRICS = {
        'runs': 0,
        'elapsed': 0,
        'elapsed_waiting': 0,
        'failures': 0,
        'successes': 0,
    }

    DEFAULT_JITTER = fractions.Fraction(5, 100)
    """
    Default jitter percentage the built-in strategies (that have jitter
    support) will use.
    """

    BUILT_IN_STRATEGIES = {
        'last_started': (
            _last_started_strategy,
            _now_plus_periodicity,
        ),
        'last_started_jitter': (
            _add_jitter(DEFAULT_JITTER)(_last_started_strategy),
            _now_plus_periodicity,
        ),
        'last_finished': (
            _last_finished_strategy,
            _now_plus_periodicity,
        ),
        'last_finished_jitter': (
            _add_jitter(DEFAULT_JITTER)(_last_finished_strategy),
            _now_plus_periodicity,
        ),
    }
    """
    Built in scheduling strategies (used to determine when next to run
    a periodic callable).

    The first element is the strategy to use after the initial start
    and the second element is the strategy to use for the initial start.

    These are made somewhat pluggable so that we can *easily* add-on
    different types later (perhaps one that uses a cron-style syntax
    for example).
    """

    @classmethod
    def create(cls, objects, exclude_hidden=True,
               log=None, executor_factory=None,
               cond_cls=threading.Condition, event_cls=threading.Event,
               schedule_strategy='last_started'):
        """Automatically creates a worker by analyzing object(s) methods.

        Only picks up methods that have been tagged/decorated with
        the :py:func:`.periodic` decorator (does not match against private
        or protected methods unless explicitly requested to).

        :param objects: the objects to introspect for decorated members
        :type objects: iterable
        :param exclude_hidden: exclude hidden members (ones that start with
                               an underscore)
        :type exclude_hidden: bool
        :param log: logger to use when creating a new worker (defaults
                    to the module logger if none provided), it is currently
                    only used to report callback failures (if they occur)
        :type log: logger
        :param executor_factory: factory callable that can be used to generate
                                 executor objects that will be used to
                                 run the periodic callables (if none is
                                 provided one will be created that uses
                                 the :py:class:`~futurist.SynchronousExecutor`
                                 class)
        :type executor_factory: callable
        :param cond_cls: callable object that can
                          produce ``threading.Condition``
                          (or compatible/equivalent) objects
        :type cond_cls: callable
        :param event_cls: callable object that can produce ``threading.Event``
                          (or compatible/equivalent) objects
        :type event_cls: callable
        :param schedule_strategy: string to select one of the built-in
                                  strategies that can return the
                                  next time a callable should run
        :type schedule_strategy: string
        """
        callables = []
        for obj in objects:
            for (name, member) in inspect.getmembers(obj):
                if name.startswith("_") and exclude_hidden:
                    continue
                if six.callable(member):
                    missing_attrs = _check_attrs(member)
                    if not missing_attrs:
                        # These do not support custom args, kwargs...
                        callables.append((member,
                                          cls._NO_OP_ARGS,
                                          cls._NO_OP_KWARGS))
        return cls(callables, log=log, executor_factory=executor_factory,
                   cond_cls=cond_cls, event_cls=event_cls,
                   schedule_strategy=schedule_strategy)

    def __init__(self, callables, log=None, executor_factory=None,
                 cond_cls=threading.Condition, event_cls=threading.Event,
                 schedule_strategy='last_started'):
        """Creates a new worker using the given periodic callables.

        :param callables: a iterable of tuple objects previously decorated
                          with the :py:func:`.periodic` decorator, each item
                          in the iterable is expected to be in the format
                          of ``(cb, args, kwargs)`` where ``cb`` is the
                          decorated function and ``args`` and ``kwargs`` are
                          any positional and keyword arguments to send into
                          the callback when it is activated (both ``args``
                          and ``kwargs`` may be provided as none to avoid
                          using them)
        :type callables: iterable
        :param log: logger to use when creating a new worker (defaults
                    to the module logger if none provided), it is currently
                    only used to report callback failures (if they occur)
        :type log: logger
        :param executor_factory: factory callable that can be used to generate
                                 executor objects that will be used to
                                 run the periodic callables (if none is
                                 provided one will be created that uses
                                 the :py:class:`~futurist.SynchronousExecutor`
                                 class)
        :type executor_factory: callable
        :param cond_cls: callable object that can
                          produce ``threading.Condition``
                          (or compatible/equivalent) objects
        :type cond_cls: callable
        :param event_cls: callable object that can produce ``threading.Event``
                          (or compatible/equivalent) objects
        :type event_cls: callable
        :param schedule_strategy: string to select one of the built-in
                                  strategies that can return the
                                  next time a callable should run
        :type schedule_strategy: string
        """
        self._tombstone = event_cls()
        self._waiter = cond_cls()
        self._dead = event_cls()
        self._metrics = []
        self._callables = []
        for (cb, args, kwargs) in callables:
            if not six.callable(cb):
                raise ValueError("Periodic callback %r must be callable" % cb)
            missing_attrs = _check_attrs(cb)
            if missing_attrs:
                raise ValueError("Periodic callback %r missing required"
                                 " attributes %s" % (cb, missing_attrs))
            if cb._is_periodic:
                # Ensure these aren't none and if so replace them with
                # something more appropriate...
                if args is None:
                    args = self._NO_OP_ARGS
                if kwargs is None:
                    kwargs = self._NO_OP_KWARGS
                cb_name = utils.get_callback_name(cb)
                self._callables.append((cb, cb_name, args, kwargs))
                self._metrics.append(self._INITIAL_METRICS.copy())
        try:
            strategy = self.BUILT_IN_STRATEGIES[schedule_strategy]
            self._schedule_strategy = strategy[0]
            self._initial_schedule_strategy = strategy[1]
        except KeyError:
            valid_strategies = sorted(self.BUILT_IN_STRATEGIES.keys())
            raise ValueError("Scheduling strategy '%s' must be one of"
                             " %s selectable strategies"
                             % (schedule_strategy, valid_strategies))
        self._immediates, self._schedule = _build(
            self._callables, self._initial_schedule_strategy)
        self._log = log or LOG
        if executor_factory is None:
            executor_factory = lambda: futurist.SynchronousExecutor()
        self._executor_factory = executor_factory

    def __len__(self):
        return len(self._callables)

    def _run(self, executor):
        """Main worker run loop."""

        def _on_done(kind, cb, cb_name, index, submitted_at, fut):
            started_at, finished_at, pretty_tb = fut.result()
            metrics = self._metrics[index]
            metrics['runs'] += 1
            if pretty_tb is not None:
                how_often = cb._periodic_spacing
                self._log.error("Failed to call %s '%s' (it runs every"
                                " %0.2f seconds):\n%s", kind, cb_name,
                                how_often, pretty_tb)
                metrics['failures'] += 1
            else:
                metrics['successes'] += 1
            elapsed = max(0, finished_at - started_at)
            elapsed_waiting = max(0, started_at - submitted_at)
            metrics['elapsed'] += elapsed
            metrics['elapsed_waiting'] += elapsed_waiting
            next_run = self._schedule_strategy(cb,
                                               started_at, finished_at,
                                               metrics)
            with self._waiter:
                self._schedule.push(next_run, index)
                self._waiter.notify_all()

        while not self._tombstone.is_set():
            if self._immediates:
                # Run & schedule its next execution.
                try:
                    index = self._immediates.pop()
                except IndexError:
                    pass
                else:
                    cb, cb_name, args, kwargs = self._callables[index]
                    submitted_at = utils.now()
                    self._log.debug("Submitting immediate function '%s'",
                                    cb_name)
                    fut = executor.submit(_run_callback,
                                          cb, *args, **kwargs)
                    fut.add_done_callback(functools.partial(_on_done,
                                                            'immediate',
                                                            cb, cb_name,
                                                            index,
                                                            submitted_at))
            else:
                # Figure out when we should run next (by selecting the
                # minimum item from the heap, where the minimum should be
                # the callable that needs to run next and has the lowest
                # next desired run time).
                with self._waiter:
                    while (not self._schedule and
                           not self._tombstone.is_set()):
                        self._waiter.wait(self.MAX_LOOP_IDLE)
                    if self._tombstone.is_set():
                        break
                    submitted_at = now = utils.now()
                    next_run, index = self._schedule.pop()
                    when_next = next_run - now
                    if when_next <= 0:
                        # Run & schedule its next execution.
                        cb, cb_name, args, kwargs = self._callables[index]
                        self._log.debug("Submitting periodic function '%s'",
                                        cb_name)
                        fut = executor.submit(_run_callback,
                                              cb, *args, **kwargs)
                        fut.add_done_callback(functools.partial(_on_done,
                                                                'periodic',
                                                                cb, cb_name,
                                                                index,
                                                                submitted_at))
                    else:
                        # Gotta wait...
                        self._schedule.push(next_run, index)
                        when_next = min(when_next, self.MAX_LOOP_IDLE)
                        self._waiter.wait(when_next)

    def _on_finish(self):
        # TODO(harlowja): this may be to verbose for people?
        if not self._log.isEnabledFor(logging.DEBUG):
            return
        for index, metrics in enumerate(self._metrics):
            cb, cb_name, _args, _kwargs = self._callables[index]
            runs = metrics['runs']
            self._log.debug("Stopped running callback[%s] '%s' periodically:",
                            index, cb_name)
            self._log.debug("  Periodicity = %ss", cb._periodic_spacing)
            self._log.debug("  Runs = %s", runs)
            self._log.debug("  Failures = %s", metrics['failures'])
            self._log.debug("  Successes = %s", metrics['successes'])
            if runs > 0:
                avg_elapsed = metrics['elapsed'] / runs
                avg_elapsed_waiting = metrics['elapsed_waiting'] / runs
                self._log.debug("  Average elapsed = %0.4fs", avg_elapsed)
                self._log.debug("  Average elapsed waiting = %0.4fs",
                                avg_elapsed_waiting)

    def add(self, cb, *args, **kwargs):
        """Adds a new periodic callback to the current worker.

        :param cb: a callable object/method/function previously decorated
                   with the :py:func:`.periodic` decorator
        :type cb: callable
        """
        if not six.callable(cb):
            raise ValueError("Periodic callback %r must be callable" % cb)
        missing_attrs = _check_attrs(cb)
        if missing_attrs:
            raise ValueError("Periodic callback %r missing required"
                             " attributes %s" % (cb, missing_attrs))
        now = utils.now()
        with self._waiter:
            index = len(self._callables)
            cb_name = utils.get_callback_name(cb)
            self._callables.append((cb, cb_name, args, kwargs))
            self._metrics.append(self._INITIAL_METRICS.copy())
            if cb._periodic_run_immediately:
                self._immediates.append(index)
            else:
                next_run = self._initial_schedule_strategy(cb, now)
                self._schedule.push(next_run, index)
            self._waiter.notify_all()

    def start(self, allow_empty=False):
        """Starts running (will not return until :py:meth:`.stop` is called).

        :param allow_empty: instead of running with no callbacks raise when
                            this worker has no contained callables (this can be
                            set to true and :py:meth:`.add` can be used to add
                            new callables on demand), note that when enabled
                            and no callbacks exist this will block and
                            sleep (until either stopped or callbacks are
                            added)
        :type allow_empty: bool
        """
        if not self._callables and not allow_empty:
            raise RuntimeError("A periodic worker can not start"
                               " without any callables")
        executor = self._executor_factory()
        self._dead.clear()
        try:
            self._run(executor)
        finally:
            executor.shutdown()
            self._dead.set()
            self._on_finish()

    def stop(self):
        """Sets the tombstone (this stops any further executions)."""
        with self._waiter:
            self._tombstone.set()
            self._waiter.notify_all()

    def reset(self):
        """Resets the workers internal state."""
        self._tombstone.clear()
        self._dead.clear()
        for metrics in self._metrics:
            for k in list(six.iterkeys(metrics)):
                metrics[k] = 0
        self._immediates, self._schedule = _build(
            self._callables, self._initial_schedule_strategy)

    def wait(self, timeout=None):
        """Waits for the :py:meth:`.start` method to gracefully exit.

        An optional timeout can be provided, which will cause the method to
        return within the specified timeout. If the timeout is reached, the
        returned value will be False.

        :param timeout: Maximum number of seconds that the :meth:`.wait`
                        method should block for
        :type timeout: float/int
        """
        self._dead.wait(timeout)
        return self._dead.is_set()