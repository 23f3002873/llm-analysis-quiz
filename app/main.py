from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

from app.config import settings
from solver.solver import QuizSolver

app = FastAPI(title="LLM Analysis Quiz Endpoint")


@app.post("/quiz")
async def quiz_endpoint(req: Request):
    """
    Main quiz handler with DEBUG WRAPPER — returns traceback when an exception occurs.
    REMOVE the debug wrapper once debugging is complete.
    """

    # Try parse JSON
    try:
        payload = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Validate fields
    if not all(k in payload for k in ("email", "secret", "url")):
        raise HTTPException(status_code=400, detail="Missing fields")

    email = payload["email"]
    secret = payload["secret"]
    url = payload["url"]

    # Verify secret
    if secret != settings.SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    # DEBUG WRAPPER: return errors with traceback instead of silent 500
    try:
        solver = QuizSolver(
            email=email,
            secret=secret,
            start_url=url,
            timeout=settings.TIMEOUT_SECONDS
        )

        result = await solver.run()
        return JSONResponse(status_code=200, content=result)

    except Exception as e:
        import traceback
        tb = traceback.format_exc()

        # Return detailed debug information (TEMPORARY — REMOVE LATER)
        return JSONResponse(
            status_code=500,
            content={
                "error": str(e),
                "payload_sample": {
                    "email": email,
                    "url": url
                },
                "traceback": tb
            }
        )
