"""Shared HTTP dependencies and business errors."""

from typing import Annotated
from uuid import uuid4

from fastapi import Depends, Header, Request

from .jobs import JobStore


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def job_store(request: Request) -> JobStore:
    return request.app.state.jobs


def request_id(request: Request) -> str:
    if not hasattr(request.state, "request_id"):
        request.state.request_id = str(uuid4())
    return request.state.request_id


Store = Annotated[JobStore, Depends(job_store)]

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=100)]
