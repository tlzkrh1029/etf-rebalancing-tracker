"""Command-line interface: ``python3 -m etf_tracker [run|fetch|backfill|analyze|report]``.

Standard library only (argparse + logging).

Subcommands
-----------
``run`` (default)
    Fetch the latest holdings of every ETF, analyse them and write reports;
    what the daily GitHub Actions workflow calls.
``fetch``
    Fetch and store only.
``backfill``
    Fetch a range of past trading days for one ETF (``--etf --start --end``).
``analyze``
    Analyse the stored snapshots and, with ``--json``, print the result.
``report``
    Analyse and write the reports without fetching.

Exit codes follow :mod:`etf_tracker.pipeline`: 0 when every ETF is ok or
already up to date, 2 when some ETF failed, 3 when nothing could be
analysed.  Usage errors exit 1, including argparse syntax errors (argparse's
own default of 2 would collide with "some ETF failed").  An unexpected
exception propagates with Python's exit code 1 and a traceback.

Help texts are in Korean because the owner reads Korean; identifiers and
log messages stay in English.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any, NoReturn

from etf_tracker import pipeline
from etf_tracker.pipeline import (
    DEFAULT_ETFS,
    report_universe,
    EXIT_FAILED,
    EXIT_NOTHING_ANALYSED,
    EXIT_OK,
    run_analysis,
    run_backfill,
    run_daily,
    run_fetch,
    write_reports,
)

__all__ = ["SUBCOMMANDS", "DEFAULT_SUBCOMMAND", "EXIT_USAGE", "build_parser", "main"]

log = logging.getLogger(__name__)

SUBCOMMANDS: tuple[str, ...] = ("run", "fetch", "backfill", "analyze", "report")
DEFAULT_SUBCOMMAND = "run"
EXIT_USAGE = 1


class _Parser(argparse.ArgumentParser):
    """ArgumentParser whose usage errors exit with :data:`EXIT_USAGE`."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------


def _iso_date(text: str) -> date:
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"날짜는 YYYY-MM-DD 형식이어야 합니다: {text!r}") from None


def _common_options() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--root",
        default=".",
        help="저장소 루트 디렉터리입니다. data/와 reports/가 이 아래에 만들어집니다 (기본값: 현재 디렉터리).",
    )
    common.add_argument(
        "--etf",
        action="append",
        metavar="TICKER",
        help="처리할 ETF 티커입니다. 여러 번 지정할 수 있으며, 생략하면 SOXX, QQQ, IGV 모두를 처리합니다.",
    )
    common.add_argument(
        "--as-of",
        type=_iso_date,
        metavar="YYYY-MM-DD",
        help="가져올 보유 내역의 기준일입니다. 생략하면 발행사가 제공하는 최신 날짜를 가져옵니다.",
    )
    common.add_argument(
        "--force",
        action="store_true",
        help="이미 저장된 날짜라도 다시 내려받아 덮어씁니다.",
    )
    common.add_argument(
        "--today",
        type=_iso_date,
        metavar="YYYY-MM-DD",
        help="분석 기준일입니다. 이벤트 카운트다운과 적용 규칙을 정하고 backfill의 종료일 상한이 되며, 생략하면 UTC 오늘 날짜를 씁니다.",
    )
    common.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="로그 수준입니다 (기본값: INFO).",
    )
    common.add_argument(
        "--json",
        action="store_true",
        help="결과(분석 JSON 또는 수집 결과)를 표준 출력에 JSON으로 출력합니다.",
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    """The argparse parser (exposed for documentation and tests)."""
    common = _common_options()
    parser = _Parser(
        prog="python3 -m etf_tracker",
        description=(
            "SOXX, QQQ, IGV의 보유 내역을 내려받아 저장하고, 비중 변화를 가격 효과와 주식수 효과로 "
            "분해하며, 지수 상한 규칙에 따른 강제 매매를 계산합니다. 하위 명령을 생략하면 run을 실행합니다."
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="{run,fetch,backfill,analyze,report}", parser_class=_Parser)

    sub.add_parser(
        "run",
        parents=[common],
        help=(
            "최신 보유 내역을 수집하고 분석한 뒤 리포트를 씁니다 (일일 워크플로가 호출하는 기본 명령). "
            "새로 저장된 날짜가 없으면 리포트를 다시 쓰지 않습니다 (--force를 주면 다시 씁니다)."
        ),
    )
    sub.add_parser("fetch", parents=[common], help="보유 내역을 내려받아 저장만 합니다.")
    backfill = sub.add_parser(
        "backfill",
        parents=[common],
        help="한 ETF의 과거 거래일 구간을 차례로 내려받습니다 (--etf, --start, --end 필요).",
    )
    backfill.add_argument("--start", type=_iso_date, required=True, metavar="YYYY-MM-DD", help="시작일입니다.")
    backfill.add_argument("--end", type=_iso_date, required=True, metavar="YYYY-MM-DD", help="종료일입니다 (포함).")
    backfill.add_argument(
        "--pause",
        type=float,
        default=pipeline.DEFAULT_PAUSE_S,
        metavar="SECONDS",
        help=f"요청 사이에 쉬는 시간(초)입니다 (기본값: {pipeline.DEFAULT_PAUSE_S}).",
    )
    sub.add_parser("analyze", parents=[common], help="저장된 스냅샷을 분석합니다. --json으로 결과를 출력합니다.")
    sub.add_parser("report", parents=[common], help="수집 없이 분석하고 리포트를 다시 씁니다 (run과 달리 새 데이터가 없어도 항상 씁니다).")
    return parser


def _normalize_argv(argv: Sequence[str]) -> list[str]:
    """Insert the default subcommand when none was given.

    ``python3 -m etf_tracker --root x`` means ``run --root x``; a bare
    ``--help`` keeps the top-level help.
    """
    args = list(argv)
    for token in args:
        if token in SUBCOMMANDS:
            return args
        if token in ("-h", "--help"):
            return args
    return [DEFAULT_SUBCOMMAND, *args]


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _etfs(ns: argparse.Namespace) -> list[str]:
    chosen = ns.etf or list(DEFAULT_ETFS)
    return [e.strip().upper() for e in chosen if e and e.strip()]


def _emit_json(payload: Any, out: Any) -> None:
    out.write(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False))
    out.write("\n")
    out.flush()


def _fetch_exit_code(outcomes: list[Any]) -> int:
    return EXIT_FAILED if any(o.failed for o in outcomes) else EXIT_OK


def _analysis_exit_code(analysis: dict[str, Any], extra_failure: bool = False) -> int:
    if not analysis.get("etfs"):
        return EXIT_NOTHING_ANALYSED
    if analysis.get("errors") or extra_failure:
        return EXIT_FAILED
    return EXIT_OK


def _cmd_run(ns: argparse.Namespace, out: Any) -> int:
    outcome = run_daily(Path(ns.root), _etfs(ns), as_of=ns.as_of, force=ns.force, today=ns.today)
    if outcome.reports_written:
        log.info("wrote %d report file(s): %s", len(outcome.report_paths), outcome.report_reason)
    if ns.json:
        _emit_json(outcome.analysis, out)
    return outcome.exit_code


def _cmd_fetch(ns: argparse.Namespace, out: Any) -> int:
    outcomes = run_fetch(Path(ns.root), _etfs(ns), as_of=ns.as_of, force=ns.force)
    if ns.json:
        _emit_json([o.to_dict() for o in outcomes], out)
    return _fetch_exit_code(outcomes)


def _cmd_backfill(ns: argparse.Namespace, out: Any) -> int:
    etfs = _etfs(ns)
    if ns.etf is None or len(etfs) != 1:
        log.error("backfill needs exactly one --etf")
        return EXIT_USAGE
    if ns.end < ns.start:
        log.error("backfill --end %s is before --start %s", ns.end.isoformat(), ns.start.isoformat())
        return EXIT_USAGE
    outcomes = run_backfill(Path(ns.root), etfs[0], ns.start, ns.end, force=ns.force, pause_s=ns.pause, today=ns.today)
    stored = sum(1 for o in outcomes if o.stored)
    log.info("backfill %s: %d dates visited, %d stored", etfs[0], len(outcomes), stored)
    if ns.json:
        _emit_json([o.to_dict() for o in outcomes], out)
    return _fetch_exit_code(outcomes)


def _cmd_analyze(ns: argparse.Namespace, out: Any) -> int:
    analysis = run_analysis(Path(ns.root), _etfs(ns), today=ns.today)
    if ns.json:
        _emit_json(analysis, out)
    return _analysis_exit_code(analysis)


def _cmd_report(ns: argparse.Namespace, out: Any) -> int:
    # Report files always describe every tracked ETF with stored data.
    analysis = run_analysis(Path(ns.root), report_universe(Path(ns.root), _etfs(ns)), today=ns.today)
    report_failed = False
    if analysis.get("etfs"):
        try:
            paths = write_reports(Path(ns.root), analysis)
            log.info("wrote %d report file(s)", len(paths))
        except Exception:  # noqa: BLE001 - surface through the exit code
            log.exception("report writing failed")
            report_failed = True
    if ns.json:
        _emit_json(analysis, out)
    return _analysis_exit_code(analysis, extra_failure=report_failed)


_COMMANDS = {
    "run": _cmd_run,
    "fetch": _cmd_fetch,
    "backfill": _cmd_backfill,
    "analyze": _cmd_analyze,
    "report": _cmd_report,
}


def main(argv: Sequence[str] | None = None, *, stdout: Any = None) -> int:
    """Entry point; returns the process exit code instead of calling ``sys.exit``."""
    parser = build_parser()
    ns = parser.parse_args(_normalize_argv(sys.argv[1:] if argv is None else argv))
    logging.basicConfig(level=getattr(logging, ns.log_level), format=_LOG_FORMAT, stream=sys.stderr)
    logging.getLogger().setLevel(getattr(logging, ns.log_level))
    out = stdout if stdout is not None else sys.stdout
    command = _COMMANDS[ns.command or DEFAULT_SUBCOMMAND]
    try:
        return command(ns, out)
    except KeyboardInterrupt:
        log.error("interrupted")
        return 130
