import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Set, Tuple
import threading

import requests

from ..factories import create_api
from ..logging_utils import build_logger
from ..core.fields import position_count
from ..selection.scoring import choose_markets, rank_markets, safe_float
from .cleanup import stop_worker_then_cancel
from .workers import run_market_worker


def run_dynamic_strategy(dynamic_config: Dict):
    logger = build_logger("DynamicSelector", dynamic_config.get("log_level", "INFO"))

    selector_cfg = dynamic_config.get("market_selector", {})
    refresh_seconds = safe_float(selector_cfg.get("refresh_seconds", 20), 20.0)
    series_ticker = selector_cfg.get("series_ticker")
    mve_filter = selector_cfg.get("mve_filter", "exclude")
    page_limit = int(selector_cfg.get("page_limit", 250))
    max_pages = int(selector_cfg.get("max_pages", 5))
    max_markets = int(selector_cfg.get("max_markets", 1250))

    selector_api = create_api(dynamic_config.get("api", {}), logger, market_ticker="DYNAMIC")
    # ticker -> (stop_event, future, worker_state)
    active_workers: Dict[str, Tuple[threading.Event, object, Dict]] = {}
    selected_since: Dict[str, float] = {}
    held_tickers: Set[str] = set()
    max_global_contracts = int(dynamic_config.get("risk", {}).get("max_global_contracts", 20))
    # Room for every selected market plus a reduce-only worker per held position.
    max_workers = int(selector_cfg.get("top_n", 8)) + max_global_contracts + 1
    last_selected_tickers: List[str] = []
    shared_risk_state = {"active_markets": 1}
    selector_backoff_seconds = 5.0
    max_selector_backoff_seconds = 120.0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        try:
            while True:
                markets: List[Dict] = []
                selected_tickers = last_selected_tickers
                scan_ok = False

                try:
                    markets = selector_api.list_all_open_markets(
                        series_ticker=series_ticker,
                        mve_filter=mve_filter,
                        page_limit=page_limit,
                        max_pages=max_pages,
                        max_markets=max_markets,
                    )
                    ranked = rank_markets(markets, selector_cfg)
                    now = time.time()
                    selected_tickers = choose_markets(ranked, selected_since, now, selector_cfg)
                    selected_since = {ticker: selected_since.get(ticker, now) for ticker in selected_tickers}
                    last_selected_tickers = selected_tickers
                    selector_backoff_seconds = 5.0
                    scan_ok = True
                except requests.exceptions.HTTPError as http_error:
                    status_code = http_error.response.status_code if http_error.response is not None else None
                    if status_code == 429:
                        logger.warning(
                            f"Selector rate-limited (429). Reusing previous selection and backing off for "
                            f"{selector_backoff_seconds:.1f}s"
                        )
                        time.sleep(selector_backoff_seconds)
                        selector_backoff_seconds = min(
                            selector_backoff_seconds * 2,
                            max_selector_backoff_seconds,
                        )
                    else:
                        logger.error(f"Selector HTTP error ({status_code}): {http_error}")
                        time.sleep(selector_backoff_seconds)
                except requests.exceptions.RequestException as request_exception:
                    logger.error(f"Selector request error: {request_exception}")
                    time.sleep(selector_backoff_seconds)

                # Positions in markets we no longer select keep a reduce-only worker
                # until flat, instead of being abandoned on deselection.
                try:
                    held_tickers = {
                        position["ticker"]
                        for position in selector_api.list_all_positions()
                        if position.get("ticker") and position_count(position) != 0
                    }
                except requests.exceptions.RequestException as request_exception:
                    logger.error(f"Position snapshot failed, keeping previous held set: {request_exception}")
                if scan_ok:
                    open_tickers = {market.get("ticker") for market in markets}
                    held_tickers = {ticker for ticker in held_tickers if ticker in open_tickers}

                selected_set = set(selected_tickers)
                reduce_only_tickers = held_tickers - selected_set
                wanted_set = selected_set | reduce_only_tickers
                shared_risk_state["active_markets"] = max(1, len(selected_tickers))
                logger.info(f"Selector found {len(markets)} open markets; selected: {selected_tickers}")
                if reduce_only_tickers:
                    logger.info(f"Reduce-only (held, not selected): {sorted(reduce_only_tickers)}")

                for ticker in list(active_workers.keys()):
                    stop_event, future, worker_state = active_workers[ticker]
                    worker_state["reduce_only"] = ticker in reduce_only_tickers
                    if ticker not in wanted_set:
                        logger.warning(f"Draining ticker {ticker}: stop worker then cancel resting orders")
                        is_clean = stop_worker_then_cancel(
                            ticker,
                            stop_event,
                            future,
                            dynamic_config,
                            logger,
                        )
                        if is_clean:
                            del active_workers[ticker]
                        else:
                            logger.error(
                                f"Could not fully clean up {ticker}; keeping worker state for next retry cycle"
                            )

                for ticker in sorted(wanted_set):
                    if ticker in active_workers and active_workers[ticker][1].done():
                        # Worker exited (e.g. exchange outage) while still wanted; restart it.
                        logger.warning(f"Worker for {ticker} exited; restarting")
                        del active_workers[ticker]
                    if ticker not in active_workers:
                        mode = "reduce-only" if ticker in reduce_only_tickers else "selected"
                        logger.info(f"Starting worker for {mode} ticker {ticker}")
                        stop_event = threading.Event()
                        worker_state = {"reduce_only": ticker in reduce_only_tickers}
                        future = executor.submit(
                            run_market_worker,
                            ticker,
                            dynamic_config,
                            stop_event,
                            shared_risk_state,
                            worker_state,
                        )
                        active_workers[ticker] = (stop_event, future, worker_state)

                time.sleep(refresh_seconds)
        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt, shutting down dynamic strategy")
        finally:
            for ticker in list(active_workers.keys()):
                stop_event, future, _ = active_workers[ticker]
                logger.warning(f"Final shutdown cleanup for {ticker}")
                stop_worker_then_cancel(ticker, stop_event, future, dynamic_config, logger)
            selector_api.logout()
