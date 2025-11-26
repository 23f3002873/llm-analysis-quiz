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
                    try:
                        visible_text = await page.inner_text("body")
                    except Exception:
                        visible_text = ""
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
            # Try any <pre> elements (not just the first)
            pres = await page.query_selector_all("pre")
            for pre in pres:
                try:
                    text = await pre.inner_text()
                except Exception:
                    continue
                # Try direct JSON
                try:
                    return json.loads(text)
                except Exception:
                    # Try base64
                    try:
                        decoded = base64.b64decode(text).decode("utf-8")
                        return json.loads(decoded)
                    except Exception:
                        continue
        except Exception:
            return None

        return None

    async def _find_submit_url(self, page) -> Optional[str]:
        """
        Robust submit-URL finder. Tries several heuristics, ordered:
        1. look for explicit absolute URLs containing '/submit'
        2. look for form[action]
        3. look for data-submit attributes
        4. look for JS fetch/XHR calls in inline scripts
        5. fallback: any URL that contains '/submit'
        Logs a small page snippet to help debugging.
        """
        try:
            body = await page.content()
        except Exception:
            return None

        # Log a short snippet to Render logs to help debugging
        try:
            snippet = body[:2000].replace("\n", " ").replace("\r", " ")
            print("PAGE_SNIPPET:", snippet)
        except Exception:
            pass

        # 1) Safe regex for absolute submit URLs (hyphen placed at end)
        try:
            pattern = r"https?://[\w./:?=&\-]+/submit[\w./:?=&\-]*"
            m = re.search(pattern, body)
            if m:
                url = m.group(0)
                print("FOUND submit URL (pattern1):", url)
                return url
        except Exception:
            pass

        # 2) Look for <form action="...">
        try:
            # run in page context to get forms' actions resolved to absolute URLs if present
            actions = await page.eval_on_selector_all(
                "form",
                "forms => forms.map(f => f.action || f.getAttribute('action'))"
            )
            for a in actions:
                if a and "/submit" in a:
                    print("FOUND submit URL (form):", a)
                    return a
        except Exception:
            pass

        # 3) data-submit attribute on any element
        try:
            el = await page.query_selector("[data-submit]")
            if el:
                attr = await el.get_attribute("data-submit")
                if attr:
                    print("FOUND submit URL (data-submit):", attr)
                    return attr
        except Exception:
            pass

        # 4) Inline scripts: look for fetch/post URLs or JSON payloads with submit keys
        try:
            scripts = await page.eval_on_selector_all("script", "scripts => scripts.map(s => s.innerText).filter(Boolean)")
            joined = " ".join(scripts)[:200000]  # cap size
            # look for fetch('https://.../submit' or fetch("https://.../submit")
            m2 = re.search(r"fetch\(['\"](https?://[^'\"\)]+/submit[^'\"\)]*)['\"]", joined)
            if m2:
                url = m2.group(1)
                print("FOUND submit URL (fetch):", url)
                return url
            # fallback: any https url in scripts containing /submit
            m3 = re.search(r"https?://[^'\"\s]+/submit[^'\"\s]*", joined)
            if m3:
                url = m3.group(0)
                print("FOUND submit URL (script-any):", url)
                return url
        except Exception:
            pass

        # 5) Looser fallback: any absolute or relative URL with '/submit' in body
        try:
            m4 = re.search(r"(https?://[^\s'\"<>]+/submit[^\s'\"<>]*)", body)
            if m4:
                url = m4.group(1)
                print("FOUND submit URL (fallback1):", url)
                return url
            # relative URL: find action="/submit" or '/submit' fragments and resolve
            m5 = re.search(r"action=['\"]([^'\"\"]*?/submit[^'\"\"]*)['\"]", body)
            if m5:
                rel = m5.group(1)
                # try to compute absolute by using the page's URL
                try:
                    origin = await page.evaluate("() => location.origin")
                    if rel.startswith("/"):
                        abs_url = origin + rel
                    else:
                        base = await page.evaluate("() => location.href")
                        abs_url = base.rsplit("/", 1)[0] + "/" + rel
                    print("FOUND submit URL (fallback2 resolved):", abs_url)
                    return abs_url
                except Exception:
                    print("FOUND submit URL (fallback2 raw):", rel)
                    return rel
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
            try:
                links = await page.eval_on_selector_all("a", "elements => elements.map(e => e.href)")
            except Exception:
                links = []
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
