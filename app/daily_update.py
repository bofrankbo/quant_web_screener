import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_module(module: str, *args: str) -> tuple[bool, str]:
    cmd = [sys.executable, "-m", module, *args]
    proc = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    output = proc.stdout or ""
    error = proc.stderr or ""
    combined = output + (error if error else "")
    return proc.returncode == 0, combined


def run_daily_update(target_date: str) -> dict:
    jobs = {
        "fetch_prices": ("scripts.fetch_prices", "--date", target_date),
        "fetch_concentration": ("scripts.fetch_concentration", "--date", target_date),
        "fetch_market_value": ("scripts.fetch_market_value", "--date", target_date),
    }

    results: dict[str, dict] = {}
    print(f"=== Daily update: {target_date} ===")
    print("[1/4] Running price, concentration, and market value fetches in parallel")

    with ThreadPoolExecutor(max_workers=3) as executor:
        future_map = {
            executor.submit(_run_module, module, *args): name
            for name, (module, *args) in jobs.items()
        }
        for future in as_completed(future_map):
            name = future_map[future]
            ok, output = future.result()
            results[name] = {"ok": ok, "output": output}
            status = "ok" if ok else "failed"
            print(f"[{name}] {status}")
            if output.strip():
                print(output.rstrip())

    print("[4/4] Sync R2 → local cache")
    sync_ok, sync_output = _run_module("scripts.sync_cache", "--all")
    results["sync_cache"] = {"ok": sync_ok, "output": sync_output}
    if sync_output.strip():
        print(sync_output.rstrip())

    success = all(item["ok"] for item in results.values())
    print("=== Done ===" if success else "=== Done with errors ===")
    results["success"] = success
    return results

