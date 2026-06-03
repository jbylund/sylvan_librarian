#!/usr/bin/env python3
"""Generate postgresql.conf from template with memory settings tuned to available memory.

Memory values are computed from the available memory with conservative ratios so that two
concurrent instances (blue/green) fit comfortably on the host.
"""

import argparse
import platform
import subprocess
from pathlib import Path
from string import Template


def get_available_memory_bytes() -> int:
    """Return available memory in bytes.

    Prefers the Docker VM's allocation (via `docker info`) so that limits set
    in Docker Desktop / colima are respected.  Falls back to host physical
    memory if Docker is not reachable.
    """
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.MemTotal}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            mem = int(result.stdout.strip())
            if mem > 0:
                return mem
    except OSError:
        pass

    if platform.system() == "Darwin":
        result = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, check=False)
        return int(result.stdout.strip())

    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024

    msg = "Could not determine available memory"
    raise RuntimeError(msg)


def fmt_mb(n_bytes: int) -> str:
    """Format bytes as a PostgreSQL memory string in MB."""
    return f"{n_bytes // (1024 * 1024)}MB"


def compute_settings(total_bytes: int) -> dict[str, str]:
    """Compute PostgreSQL memory settings sized for two concurrent instances (blue/green)."""
    # Sized to leave headroom when both blue and green postgres containers are running.
    shared_buffers = int(total_bytes * 0.15)
    effective_cache_size = int(total_bytes * 0.40)
    # maintenance_work_mem: 4% of RAM, clamped to [64 MB, 2 GB]
    maintenance_work_mem = max(
        64 * 1024 * 1024,
        min(int(total_bytes * 0.04), 2 * 1024 * 1024 * 1024),
    )
    # work_mem: 0.1% of RAM (safe under 100 connections * 4 parallel workers), minimum 16 MB
    work_mem = max(16 * 1024 * 1024, int(total_bytes * 0.001))

    return {
        "available_memory": fmt_mb(total_bytes),
        "shared_buffers": fmt_mb(shared_buffers),
        "effective_cache_size": fmt_mb(effective_cache_size),
        "maintenance_work_mem": fmt_mb(maintenance_work_mem),
        "work_mem": fmt_mb(work_mem),
    }


def main() -> None:
    """Parse args, detect available memory, and render the postgresql.conf template."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", required=True, help="path to postgresql.conf.template")
    parser.add_argument("--output", required=True, help="path to write postgresql.conf")
    args = parser.parse_args()

    total_bytes = get_available_memory_bytes()
    settings = compute_settings(total_bytes)

    template_text = Path(args.template).read_text()
    result = Template(template_text).safe_substitute(settings)
    Path(args.output).write_text(result)

    print(f"Generated {args.output}")
    for key, val in settings.items():
        print(f"  {key}: {val}")


if __name__ == "__main__":
    main()
