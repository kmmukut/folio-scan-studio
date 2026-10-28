"""Background jobs: progress, cancelling and downloads.

Route handlers moved out of app.py in 0.21. Shared state and helpers stay in
app.py and are reached as web.NAME, so tests can still swap them there.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from backend import tasks
from backend import app as web

router = APIRouter()


@router.get('/api/tasks/{task_id}')
def task_status(task_id: str, request: Request):
    task = web.owned_task(task_id, web.task_owner(request))
    return task.public(tasks.runner.position(task))


@router.get('/api/tasks/{task_id}/file')
def task_file(task_id: str, request: Request):
    task = web.owned_task(task_id, web.task_owner(request))
    if task.status != 'done' or not task.file or not task.file.is_file():
        raise HTTPException(409, 'This file is not ready yet')
    media_type = 'application/gzip' if task.file.name.endswith('.tar.gz') else 'application/pdf'
    return FileResponse(task.file, media_type=media_type, filename=task.filename,
                        headers={'Cache-Control': 'no-store'})


@router.delete('/api/tasks/{task_id}')
def cancel_task(task_id: str, request: Request):
    task = web.owned_task(task_id, web.task_owner(request))
    tasks.runner.cancel(task)
    return task.public()
