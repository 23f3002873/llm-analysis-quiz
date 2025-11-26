from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

from app.config import settings
from solver.solver import QuizSolver

app = FastAPI(title="LLM Analysis Quiz Endpoint")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/quiz")
async def quiz_endpoint(req: Request):
    """
    Production quiz handler — validates input, verifies secret,
    runs the solver, and returns the solver result.
    """
    # Parse JSON
    try:
        payload = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Validate required fields
    if not all(k in payload for k in ("email", "secret", "url")):
        raise HTTPException(status_code=400, detail="Missing fields")

    email = payload["email"]
    secret = payload["secret"]
    url = payload["url"]

    # Verify secret
    if secret != settings.SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    # Run solver
    solver = QuizSolver(
        email=email,
        secret=secret,
        start_url=url,
        timeout=settings.TIMEOUT_SECONDS
    )

    try:
        result = await solver.run()
    except Exception as e:
        # Logable by Render, but return a generic 500 message
        # (Render logs will still show the exception trace if present)
        raise HTTPException(status_code=500, detail="Solver failed to run")

    return JSONResponse(status_code=200, content=result)
