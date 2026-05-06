"""Scheduler introspection — exposes APScheduler jobs for admin monitoring."""

from fastapi import APIRouter, Depends

from app.dependencies import verify_api_key
from app.scheduler import scheduler

router = APIRouter(prefix="/scheduler", tags=["Scheduler"])


@router.get("/jobs")
async def list_jobs(_: str = Depends(verify_api_key)):
    """Return all APScheduler jobs (admin-only via internal API key)."""
    jobs = []
    for j in scheduler.get_jobs():
        jobs.append({
            "id": j.id,
            "name": j.name,
            "next_run_time": j.next_run_time.isoformat() if j.next_run_time else None,
            "trigger": str(j.trigger),
        })
    return jobs
