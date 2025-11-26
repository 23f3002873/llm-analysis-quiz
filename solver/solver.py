import asyncio
from playwright.async_api import async_playwright
import httpx
import re
import json
import base64
import tempfile
import os
import time
from typing import Optional

import pandas as pd
import pdfplumber


class QuizSolver:
    def __init__(self, email: str, secret: str, start_url: str, timeout: int = 180):
        self.email = email
        self.secret = secret
        self.start_url = start_url
        self.timeout = timeout
        self.client = httpx.AsyncClient(timeout=30.0)

    async def run(self):
        start_time = time.time()
        next_url = self.start_url
        last_response = {"correct": False, "reason": "Not attempted"}

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()

            while next_url and (time.time() - start_time) < self.timeout:

                print(f"Visiting: {next_url}")
                page = await context.new_page()
                await page.goto(next_url, wait_until="networkidle")

                # Extract possible quiz instructions
                json_blob = await self._find_json_in_page(page)

                answer = None
                submit_url = None

                # Try finding submit URL
                submit_url = await self._find_submit_url(page)

                # If JSON blob exists, try solving from it
                if json_blob:
                    answer = await self._solve_from_json_blob(json_blob, page)

                # If still no answer, try heuristic extraction from visible text
                if answer is None:
                    visible_text = await page.inner_text("body")
                    answer = await self._heuristic_solve_text(visible_text, page)

                # Resolve submit URL if still missing
                if submit_url is None:
                    submit_url = await self._find_submit_url(page)

                # If answer + submit URL available → submit
                if submit_url and answer is not None:
                    payload = {
                        "email": self.email,
                        "secret": self.secret,
                        "url": next_url,
                        "answer": answer,
                    }

                    print("Submitting answer to", submit_url, payload)

                    try:
                        resp = await self.client.post(submit_url, json=payload)
                        resp.raise_for_status()
                        parsed = resp.json()
                        last_response = parsed

                        # Move to next URL
                        next_url = parsed.get("url")

                    except Exception as e:
                        print("Submission failed:", e)
                        last_response = {"correct": False, "reason": str(e)}
                        break

                else:
                    last_response = {
                        "correct": False,
                        "reason": "Could not find submit URL or compute answer",
                    }
                    break

                await page.close()

            await browser.close()
            await self.client.aclose()

        return last_response

    async def _find_json_in_page(self, page) -> Optional[dict]:
        """
        Extract <pre> JSON blocks or base64-encoded JSON.
        """
        try:
            pre = await page.query_selector("pre")
            if pre:
                text = await pre.inner_text()
                # Try direct JSON
                try:
                    return json.loads(text)
                except Exception:
                    # Try base64
                    try:
                        decoded = base64.b64decode(text).decode("utf-8")
                        return json.loads(decoded)
                    except Exception:
                        return None
        except Exception:
            return None

        return None

    async def _find_submit_url(self, page) -> Optional[str]:
        """
        Look for URLs ending in /submit or containing /submit in HTML.
        """
        body = await page.content()
        m = re.search(r"https?://[\\w\\-./:?=&]+/submit[\\w\\-./:?=&]*", body)
        if m:
            return m.group(0)

        # Try data-submit attr
        try:
            element = await page.query_selector("[data-submit]")
            if element:
                return await element.get_attribute("data-submit")
        except Exception:
            pass

        return None

    async def _solve_from_json_blob(self, blob: dict, page):
        """
        Handle JSON instructions — PDF tasks, direct answers, etc.
        """

        # Common pattern: blob contains URL to a file
        download_url = blob.get("url") or blob.get("file")
        if download_url:
            local_path = await self._download_file(download_url)

            if local_path and local_path.lower().endswith(".pdf"):
                val = await self._sum_pdf_table_column(local_path)
                if val is not None:
                    return val

        # If blob already contains answer
        if "answer" in blob:
            return blob["answer"]

        return None

    async def _heuristic_solve_text(self, text: str, page):
        """
        Fallback heuristic for sample questions like:
        "What is the sum of the value column on page 2?"
        """

        if "sum of the" in text.lower() and "value" in text.lower():
            # Try finding a PDF link
            links = await page.eval_on_selector_all("a", "elements => elements.map(e => e.href)")
            for link in links:
                if link and link.lower().endswith(".pdf"):
                    local_path = await self._download_file(link)
                    if local_path:
                        return await self._sum_pdf_table_column(local_path, page_number=2)

        return None

    async def _download_file(self, url: str) -> Optional[str]:
        try:
            r = await self.client.get(url)
            r.raise_for_status()

            suffix = os.path.splitext(url)[1]
            fd, path = tempfile.mkstemp(suffix=suffix)

            with os.fdopen(fd, "wb") as f:
                f.write(r.content)

            return path

        except Exception as e:
            print("Download failed", e)
            return None

    async def _sum_pdf_table_column(self, pdf_path: str, page_number: int = 2, column_name: str = "value"):
        """
        Extracts a table from PDF page and sums a column.
        """
        try:
            with pdfplumber.open(pdf_path) as pdf:
                page_index = page_number - 1

                if 0 <= page_index < len(pdf.pages):
                    page = pdf.pages[page_index]
                    table = page.extract_table()

                    if table:
                        df = pd.DataFrame(table[1:], columns=table[0])

                        # Case-insensitive match
                        matches = [c for c in df.columns if c.lower() == column_name]
                        if matches:
                            col = matches[0]
                            df[col] = pd.to_numeric(
                                df[col].str.replace(r'[^0-9.\-]', '', regex=True),
                                errors="coerce",
                            )
                            return int(df[col].sum(skipna=True))

                        # Fallback: sum numeric columns
                        numeric = df.apply(
                            lambda s: pd.to_numeric(
                                s.str.replace(r'[^0-9.\-]', '', regex=True),
                                errors="coerce",
                            )
                        )
                        return int(numeric.sum(axis=1).sum())

            return None

        except Exception as e:
            print("PDF parsing failed", e)
            return None
