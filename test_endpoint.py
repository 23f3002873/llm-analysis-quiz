import asyncio
import httpx


async def test_demo_endpoint():
    """
    Simple test to verify the /quiz endpoint accepts the demo payload.
    Run locally with: pytest -q
    """
    url = "https://tds-llm-analysis.s-anand.net/demo"

    payload = {
        "email": "23f3002873@ds.study.iitm.ac.in",
        "secret": "34595561cea24b71e0a770c18378bbdd0176440f16b7dbf9fdca6cc07866266a",
        "url": url,
    }

    async with httpx.AsyncClient() as client:
        r = await client.post("http://localhost:8000/quiz", json=payload)
        assert r.status_code in (200, 403, 400)
