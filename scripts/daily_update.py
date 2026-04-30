import argparse
from datetime import date

from app.daily_update import run_daily_update


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=str(date.today()), help="YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    result = run_daily_update(args.date)
    raise SystemExit(0 if result.get("success", False) else 1)


if __name__ == "__main__":
    main()
