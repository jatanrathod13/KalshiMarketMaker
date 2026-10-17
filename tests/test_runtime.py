"""Tests for runtime: order cleanup, worker safety gate, and the dynamic selector loop."""

import threading
from concurrent.futures import Future, TimeoutError

import pytest
import requests

from kalshi_market_maker.runtime import cleanup, dynamic, workers


class FakeLogger:
    def __init__(self):
        self.messages = []

    def _log(self, level, msg):
        self.messages.append((level, msg))

    def info(self, msg):
        self._log("info", msg)

    def warning(self, msg):
        self._log("warning", msg)

    def error(self, msg):
        self._log("error", msg)

    def errors(self):
        return [m for level, m in self.messages if level == "error"]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(cleanup.time, "sleep", lambda s: None)


# --- cleanup.cancel_resting_orders_for_ticker ---------------------------------


class FakeOrdersAPI:
    """get_orders returns successive snapshots; the last one repeats."""

    def __init__(self, snapshots, cancel_error=None, get_error=None):
        self.snapshots = list(snapshots)
        self.cancelled = []
        self.logged_out = False
        self.cancel_error = cancel_error
        self.get_error = get_error

    def get_orders(self):
        if self.get_error:
            raise self.get_error
        return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]

    def cancel_order(self, order_id):
        if self.cancel_error:
            raise self.cancel_error
        self.cancelled.append(order_id)

    def logout(self):
        self.logged_out = True


def patch_api(monkeypatch, api):
    monkeypatch.setattr(cleanup, "create_api", lambda *a, **k: api)


def test_cancel_returns_true_when_no_orders(monkeypatch):
    api = FakeOrdersAPI([[]])
    patch_api(monkeypatch, api)
    assert cleanup.cancel_resting_orders_for_ticker("T", {}, FakeLogger()) is True
    assert api.cancelled == []
    assert api.logged_out


def test_cancel_cancels_orders_then_verifies_clean(monkeypatch):
    api = FakeOrdersAPI([[{"order_id": "a"}, {"order_id": "b"}, {"no_id": 1}], []])
    patch_api(monkeypatch, api)
    assert cleanup.cancel_resting_orders_for_ticker("T", {}, FakeLogger()) is True
    assert api.cancelled == ["a", "b"]
    assert api.logged_out


def test_cancel_fails_when_orders_persist(monkeypatch):
    api = FakeOrdersAPI([[{"order_id": "a"}]])
    patch_api(monkeypatch, api)
    logger = FakeLogger()
    assert cleanup.cancel_resting_orders_for_ticker("T", {}, logger, max_attempts=2) is False
    assert api.cancelled == ["a", "a"]
    assert any("Cleanup incomplete" in m for m in logger.errors())
    assert api.logged_out


def test_cancel_survives_individual_cancel_errors(monkeypatch):
    api = FakeOrdersAPI([[{"order_id": "a"}]], cancel_error=requests.exceptions.ConnectionError("boom"))
    patch_api(monkeypatch, api)
    assert cleanup.cancel_resting_orders_for_ticker("T", {}, FakeLogger(), max_attempts=1) is False


def test_cancel_returns_false_when_api_unreachable(monkeypatch):
    api = FakeOrdersAPI([[]], get_error=requests.exceptions.ConnectionError("down"))
    patch_api(monkeypatch, api)
    logger = FakeLogger()
    assert cleanup.cancel_resting_orders_for_ticker("T", {}, logger, max_attempts=2) is False
    assert any("verification failed" in m for m in logger.errors())
    assert api.logged_out


# --- cleanup.stop_worker_then_cancel ------------------------------------------


def test_stop_worker_sets_event_then_cancels(monkeypatch):
    calls = []
    monkeypatch.setattr(cleanup, "cancel_resting_orders_for_ticker", lambda t, c, l: calls.append(t) or True)
    event, future = threading.Event(), Future()
    future.set_result(None)
    assert cleanup.stop_worker_then_cancel("T", event, future, {}, FakeLogger()) is True
    assert event.is_set()
    assert calls == ["T"]


def test_stop_worker_defers_cleanup_on_timeout(monkeypatch):
    calls = []
    monkeypatch.setattr(cleanup, "cancel_resting_orders_for_ticker", lambda *a: calls.append(a) or True)

    class Stuck:
        def result(self, timeout):
            raise TimeoutError()

    logger = FakeLogger()
    config = {"market_selector": {"worker_shutdown_timeout_seconds": 1}}
    assert cleanup.stop_worker_then_cancel("T", threading.Event(), Stuck(), config, logger) is False
    assert calls == []
    assert any("did not stop" in m for m in logger.errors())


def test_stop_worker_still_cancels_after_worker_error(monkeypatch):
    monkeypatch.setattr(cleanup, "cancel_resting_orders_for_ticker", lambda *a: True)
    future = Future()
    future.set_exception(RuntimeError("crashed"))
    logger = FakeLogger()
    assert cleanup.stop_worker_then_cancel("T", threading.Event(), future, {}, logger) is True
    assert any("exited with error" in m for m in logger.errors())


# --- workers ------------------------------------------------------------------


@pytest.mark.parametrize(
    "ticker,data,expected",
    [
        ("KXFOO-1", {}, False),
        ("KXFOO-1", {"market_type": "binary", "mve_selected_legs": []}, False),
        ("KXMVEFOO", {}, True),
        ("kxmvefoo", {}, True),
        ("KXFOO", {"mve_collection_ticker": "C"}, True),
        ("KXFOO", {"mve_selected_legs": [1]}, True),
        ("KXFOO", {"strike_type": "Functional"}, True),
        ("KXFOO", {"market_type": "scalar"}, True),
    ],
)
def test_is_unsupported_market(ticker, data, expected):
    assert workers._is_unsupported_market(ticker, data) is expected


class FakeWorkerAPI:
    def __init__(self, market=None, error=None):
        self.market = market or {}
        self.error = error
        self.logged_out = False

    def get_market(self, ticker):
        if self.error:
            raise self.error
        return {"market": self.market}

    def logout(self):
        self.logged_out = True


class FakeMaker:
    def __init__(self, error=None):
        self.error = error
        self.ran_with = None

    def run(self, dt, stop_event=None):
        self.ran_with = (dt, stop_event)
        if self.error:
            raise self.error


def patch_worker(monkeypatch, api, maker):
    created = []
    monkeypatch.setattr(workers, "create_api", lambda *a, **k: api)
    monkeypatch.setattr(workers, "create_market_maker", lambda *a, **k: created.append(a) or maker)
    return created


def test_worker_refuses_unsupported_market(monkeypatch):
    api, maker = FakeWorkerAPI({"market_type": "scalar"}), FakeMaker()
    created = patch_worker(monkeypatch, api, maker)
    workers.run_market_worker("T", {}, threading.Event())
    assert created == []
    assert maker.ran_with is None
    assert api.logged_out


def test_worker_refuses_when_market_check_fails(monkeypatch):
    api, maker = FakeWorkerAPI(error=requests.exceptions.ConnectionError("x")), FakeMaker()
    created = patch_worker(monkeypatch, api, maker)
    workers.run_market_worker("T", {}, threading.Event())
    assert created == []
    assert api.logged_out


def test_worker_runs_maker_with_dt_and_stop_event(monkeypatch):
    api, maker = FakeWorkerAPI(), FakeMaker()
    patch_worker(monkeypatch, api, maker)
    stop = threading.Event()
    workers.run_market_worker("T", {"dt": 5.0}, stop)
    assert maker.ran_with == (5.0, stop)
    assert api.logged_out


def test_worker_swallows_maker_errors_and_logs_out(monkeypatch):
    api, maker = FakeWorkerAPI(), FakeMaker(error=RuntimeError("boom"))
    patch_worker(monkeypatch, api, maker)
    workers.run_market_worker("T", {}, threading.Event())
    assert api.logged_out


# --- dynamic.run_dynamic_strategy ---------------------------------------------


def open_market(ticker, volume="1000"):
    return {
        "ticker": ticker,
        "market_type": "binary",
        "yes_bid_dollars": "0.45",
        "yes_ask_dollars": "0.55",
        "yes_bid_size_fp": "10",
        "yes_ask_size_fp": "10",
        "volume_24h_fp": volume,
    }


class FakeSelectorAPI:
    def __init__(self, scans, positions=None):
        self.scans = list(scans)  # each item: list of markets or an exception
        self.positions = positions if positions is not None else [[]]
        self.logged_out = False

    def list_all_open_markets(self, **kwargs):
        scan = self.scans.pop(0) if len(self.scans) > 1 else self.scans[0]
        if isinstance(scan, Exception):
            raise scan
        return scan

    def list_all_positions(self):
        snap = self.positions.pop(0) if len(self.positions) > 1 else self.positions[0]
        if isinstance(snap, Exception):
            raise snap
        return snap

    def logout(self):
        self.logged_out = True


class Harness:
    """Runs the strategy for a fixed number of loop iterations (stopped via KeyboardInterrupt in sleep)."""

    def __init__(self, monkeypatch, selector_api, iterations=1):
        self.started = []  # (ticker, worker_state snapshot at start)
        self.worker_states = {}
        self.drained = []
        self.sleeps = []
        self.drain_ok = True
        self.iterations = iterations
        self.api = selector_api

        def fake_worker(ticker, config, stop_event, shared, worker_state):
            self.started.append((ticker, dict(worker_state)))
            self.worker_states[ticker] = worker_state
            stop_event.wait(5)

        def fake_stop(ticker, stop_event, future, config, logger):
            self.drained.append(ticker)
            stop_event.set()
            return self.drain_ok

        def fake_sleep(seconds):
            self.sleeps.append(seconds)
            if len(self.sleeps) >= self.iterations:
                raise KeyboardInterrupt

        monkeypatch.setattr(dynamic, "create_api", lambda *a, **k: selector_api)
        monkeypatch.setattr(dynamic, "run_market_worker", fake_worker)
        monkeypatch.setattr(dynamic, "stop_worker_then_cancel", fake_stop)
        monkeypatch.setattr(dynamic.time, "sleep", fake_sleep)

    def run(self, cfg=None):
        base = {"market_selector": {"top_n": 2, "refresh_seconds": 1, "min_hold_seconds": 0, "retention_buffer": 0}}
        base["market_selector"].update((cfg or {}).get("market_selector", {}))
        dynamic.run_dynamic_strategy(base)

    def started_tickers(self):
        return [t for t, _ in self.started]


def test_dynamic_starts_workers_for_top_n_and_cleans_up_on_exit(monkeypatch):
    api = FakeSelectorAPI([[open_market("A", "3000"), open_market("B", "2000"), open_market("C", "100")]])
    h = Harness(monkeypatch, api)
    h.run({"market_selector": {"min_volume_24h": 0}})
    assert sorted(h.started_tickers()) == ["A", "B"]
    assert sorted(h.drained) == ["A", "B"]  # final shutdown cleanup
    assert api.logged_out
    assert all(state["reduce_only"] is False for _, state in h.started)


def test_dynamic_drains_deselected_worker(monkeypatch):
    first = [open_market("A", "3000"), open_market("B", "2000")]
    second = [open_market("C", "9000"), open_market("D", "8000")]
    api = FakeSelectorAPI([first, second])
    h = Harness(monkeypatch, api, iterations=2)
    h.run({"market_selector": {"min_volume_24h": 0}})
    assert set(h.started_tickers()) == {"A", "B", "C", "D"}
    # A and B drained once on deselection, plus final cleanup of C and D
    assert h.drained[:2] == ["A", "B"]
    assert sorted(h.drained[2:]) == ["C", "D"]


def test_dynamic_keeps_worker_state_when_drain_fails(monkeypatch):
    first = [open_market("A", "3000"), open_market("B", "2000")]
    second = [open_market("C", "9000"), open_market("D", "8000")]
    api = FakeSelectorAPI([first, second, second])
    h = Harness(monkeypatch, api, iterations=3)
    h.drain_ok = False
    h.run({"market_selector": {"min_volume_24h": 0}})
    # A and B are retried on the next cycle because cleanup never succeeded
    assert h.drained.count("A") >= 2


def test_dynamic_held_unselected_position_gets_reduce_only_worker(monkeypatch):
    markets = [open_market("A", "3000"), open_market("B", "2000"), open_market("HELD", "10")]
    api = FakeSelectorAPI([markets], positions=[[{"ticker": "HELD", "position_fp": "5.00"}]])
    h = Harness(monkeypatch, api)
    h.run({"market_selector": {"top_n": 2, "min_volume_24h": 100}})
    states = dict(h.started)
    assert states["HELD"]["reduce_only"] is True
    assert states["A"]["reduce_only"] is False


def test_dynamic_ignores_flat_positions_and_closed_markets(monkeypatch):
    markets = [open_market("A", "3000")]
    positions = [[{"ticker": "FLAT", "position_fp": "0.00"}, {"ticker": "GONE", "position_fp": "3.00"}]]
    api = FakeSelectorAPI([markets], positions=positions)
    h = Harness(monkeypatch, api)
    h.run({"market_selector": {"min_volume_24h": 0}})
    assert h.started_tickers() == ["A"]


def test_dynamic_selected_ticker_becomes_reduce_only_flag_flips(monkeypatch):
    # B is selected first, then drops out of the top_n while still held.
    first = [open_market("A", "3000"), open_market("B", "2000")]
    second = [open_market("A", "3000"), open_market("C", "2500"), open_market("B", "10")]
    api = FakeSelectorAPI([first, second], positions=[[{"ticker": "B", "position_fp": "2"}]])
    h = Harness(monkeypatch, api, iterations=2)
    h.run({"market_selector": {"top_n": 2, "min_volume_24h": 100}})
    assert h.worker_states["B"]["reduce_only"] is True
    assert "B" not in h.drained[:1]


def test_dynamic_429_reuses_previous_selection_and_backs_off(monkeypatch):
    response = requests.Response()
    response.status_code = 429
    rate_limited = requests.exceptions.HTTPError(response=response)
    api = FakeSelectorAPI([[open_market("A", "3000")], rate_limited, rate_limited])
    h = Harness(monkeypatch, api, iterations=5)
    h.run({"market_selector": {"min_volume_24h": 0}})
    assert h.started_tickers() == ["A"]  # no restart, selection retained
    assert "A" not in h.drained[:-1]
    backoffs = [s for s in h.sleeps if s >= 5]
    assert backoffs[:2] == [5.0, 10.0]


def test_dynamic_other_errors_do_not_crash_loop(monkeypatch):
    api = FakeSelectorAPI([requests.exceptions.ConnectionError("down"), [open_market("A", "3000")]])
    h = Harness(monkeypatch, api, iterations=3)
    h.run({"market_selector": {"min_volume_24h": 0}})
    assert h.started_tickers() == ["A"]


def test_dynamic_position_snapshot_failure_keeps_previous_held_set(monkeypatch):
    markets = [open_market("A", "3000"), open_market("B", "2000"), open_market("HELD", "10")]
    positions = [[{"ticker": "HELD", "position_fp": "5"}], requests.exceptions.ConnectionError("x")]
    api = FakeSelectorAPI([markets], positions=positions)
    h = Harness(monkeypatch, api, iterations=2)
    h.run({"market_selector": {"top_n": 2, "min_volume_24h": 100}})
    assert "HELD" not in h.drained[:-3]  # still held after failed snapshot, so never drained mid-run
    assert h.started_tickers().count("HELD") == 1


def test_dynamic_restarts_finished_worker(monkeypatch):
    api = FakeSelectorAPI([[open_market("A", "3000")]])
    h = Harness(monkeypatch, api, iterations=2)

    def quick_worker(ticker, *a):
        h.started.append((ticker, {}))  # exits immediately

    monkeypatch.setattr(dynamic, "run_market_worker", quick_worker)
    h.run({"market_selector": {"min_volume_24h": 0}})
    assert h.started_tickers().count("A") == 2
