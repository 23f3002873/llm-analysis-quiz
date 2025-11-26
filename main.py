from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import json

from app.config import settings
from solver.solver import QuizSolver

app = FastAPI(title="LLM Analysis Quiz Endpoint")


class QuizRequest(BaseModel):
    email: str
    secret: str
    url: str


@app.post("/quiz")
async def quiz_endpoint(req: Request):
    # Parse JSON
    try:
        payload = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Required fields
    if not all(k in payload for k in ("email", "secret", "url")):
        raise HTTPException(status_code=400, detail="Missing fields")

    email = payload["email"]
    secret = payload["secret"]
    url = payload["url"]

    # Verify secret
    if secret != settings.SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    # Start solver
    solver = QuizSolver(
        email=email,
        secret=secret,
        start_url=url,
        timeout=settings.TIMEOUT_SECONDS
    )

    result = await solver.run()

    # Return final result from solver
    return JSONResponse(status_code=200, content=result)
