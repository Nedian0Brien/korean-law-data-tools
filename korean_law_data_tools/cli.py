import argparse
import os

from src.scraper.LawScraper import main


def parse_args() -> argparse.Namespace:
    default_csv = os.path.join(
        os.path.dirname(__file__),
        "..",
        "src",
        "scraper",
        "국가법령정보센터_법령목록(반출).csv",
    )

    parser = argparse.ArgumentParser(description="국가법령정보센터 법령 수집기")
    parser.add_argument("--oc-id", default="leegy76", help="법제처 Open API 인증키")
    parser.add_argument(
        "--mode",
        default="target",
        choices=["full", "target", "revision", "list_only", "update"],
        help="수집 모드 (기본값: target)",
    )
    parser.add_argument("--start-date", default=None, help="시행일자 범위 시작 (YYYYMMDD)")
    parser.add_argument("--end-date", default=None, help="시행일자 범위 종료 (YYYYMMDD)")
    parser.add_argument("--target-md", default=os.path.normpath(default_csv), help="target 모드용 법령 목록 파일")
    parser.add_argument("--test", action="store_true", help="테스트 모드 (법령당 3건만 수집)")
    parser.add_argument("--no-annexes", action="store_true", help="별표/서식 수집 안 함")
    return parser.parse_args()


def cli() -> None:
    args = parse_args()
    main(
        oc_id=args.oc_id,
        mode=args.mode,
        start_date=args.start_date,
        end_date=args.end_date,
        target_md_path=args.target_md,
        test_mode=args.test,
        collect_annexes=False if args.no_annexes else None,
    )


if __name__ == "__main__":
    cli()
