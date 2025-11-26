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
                page = await context.new_page()
                await page.goto(next_url, wait_until="networkidle")

                # Try common sources of instructions
                json_blob = await self._find_json_in_page(page)

                answer = None
                submit_url = None

                # Find submit URL
                submit_url = await self._find_submit_url(page)

                # Attempt to get answer from JSON blob
                if json_blob:
                    answer = await self._solve_from_json_blob(json_blob, page)

                # Fallback heuristics
                if answer is None:
                    try:
                        visible_text = await page.inner_text("body")
                    except Exception:
                        visible_text = ""
                    answer = await self._heuristic_solve_text(visible_text, page)

                # Re-check submit_url if still missing
                if submit_url is None:
                    submit_url = await self._find_submit_url(page)

                # Submit if we have both
                if submit_url and answer is not None:
                    payload = {
                        "email": self.email,
                        "secret": self.secret,
                        "url": next_url,
                        "answer": answer,
                    }

                    try:
                        resp = await self.client.post(submit_url, json=payload)
                        resp.raise_for_status()
                        parsed = resp.json()
                        last_response = parsed
                        next_url = parsed.get("url")
                    except Exception as e:
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
        try:
            pres = await page.query_selector_all("pre")
            for pre in pres:
                try:
                    text = await pre.inner_text()
                except Exception:
                    continue

                # direct JSON
                try:
                    return json.loads(text)
                except Exception:
                    pass

                # strip tags then try JSON
                try:
                    cleaned = re.sub(r"<[^>]+>", "", text).strip()
                    return json.loads(cleaned)
                except Exception:
                    pass

                # base64 decode attempt
                try:
                    decoded = base64.b64decode(text).decode("utf-8")
                    return json.loads(decoded)
                except Exception:
                    pass

                # try to extract answer field from text
                m = re.search(r'"answer"\s*:\s*"([^"]*)"', text)
                if m:
                    return {"answer": m.group(1)}

                m2 = re.search(r'["\']?answer["\']?\s*[:=]\s*["\']?([^"\',\}\]]+)', text, flags=re.IGNORECASE)
                if m2:
                    return {"answer": m2.group(1).strip()}
        except Exception:
            return None
        return None

    async def _find_submit_url(self, page) -> Optional[str]:
        try:
            body = await page.content()
        except Exception:
            return None

        # 1) absolute URL pattern
        try:
            pattern = r"https?://[\w./:?=&\-]+/submit[\w./:?=&\-]*"
            m = re.search(pattern, body)
            if m:
                return m.group(0)
        except Exception:
            pass

        # 2) forms
        try:
            actions = await page.eval_on_selector_all(
                "form",
                "forms => forms.map(f => f.action || f.getAttribute('action')).filter(Boolean)"
            )
            for a in actions:
                if a and "/submit" in a:
                    return a
        except Exception:
            pass

        # 3) data-submit attribute
        try:
            element = await page.query_selector("[data-submit]")
            if element:
                attr = await element.get_attribute("data-submit")
                if attr:
                    return attr
        except Exception:
            pass

        # 4) anchors
        try:
            anchors = await page.eval_on_selector_all(
                "a",
                "els => els.map(e => e.href || e.getAttribute('href')).filter(Boolean).slice(0,200)"
            )
            for a in anchors:
                if a and "/submit" in a:
                    return a
        except Exception:
            pass

        # 5) inline scripts (fetch)
        try:
            scripts = await page.eval_on_selector_all("script", "scripts => scripts.map(s => s.innerText).filter(Boolean)")
            joined = " ".join(scripts)[:200000]
            m2 = re.search(r"fetch\(['\"](https?://[^'\"\)]+/submit[^'\"\)]*)['\"]", joined)
            if m2:
                return m2.group(1)
            m3 = re.search(r"https?://[^'\"\s]+/submit[^'\"\s]*", joined)
            if m3:
                return m3.group(0)
        except Exception:
            pass

        # 6) dynamic origin markers (span.origin)
        try:
            has_origin = await page.eval_on_selector("span.origin", "s => !!s")
            if has_origin:
                try:
                    origin = await page.evaluate("() => (document.querySelector('span.origin') || {}).textContent || location.origin")
                    if origin:
                        origin = origin.rstrip("/")
                        return f"{origin}/submit"
                except Exception:
                    pass
        except Exception:
            pass

        # 7) meta refresh
        try:
            metas = await page.eval_on_selector_all("meta[http-equiv='refresh'], meta[http-equiv='Refresh']", "els => els.map(e => e.getAttribute('content')).filter(Boolean)")
            for m in metas:
                if "/submit" in (m or ""):
                    return m
        except Exception:
            pass

        # 8) fallback
        try:
            m4 = re.search(r"(https?://[^\s'\"<>]+/submit[^\s'\"<>]*)", body)
            if m4:
                return m4.group(1)
        except Exception:
            pass

        return None

    async def _solve_from_json_blob(self, blob: dict, page):
        if isinstance(blob, dict) and "answer" in blob:
            return blob["answer"]

        if isinstance(blob, dict):
            download_url = blob.get("url") or blob.get("file")
            if download_url:
                local_path = await self._download_file(download_url)
                if local_path and local_path.lower().endswith(".pdf"):
                    val = await self._sum_pdf_table_column(local_path)
                    if val is not None:
                        return val

        try:
            text_blob = str(blob)
            m = re.search(r'"answer"\s*:\s*"([^"]*)"', text_blob)
            if m:
                return m.group(1)
        except Exception:
            pass

        return None

    async def _heuristic_solve_text(self, text: str, page):
        if not text:
            return None

        if "sum of the" in text.lower() and "value" in text.lower():
            try:
                links = await page.eval_on_selector_all("a", "elements => elements.map(e => e.href)")
            except Exception:
                links = []
            for link in links:
                if link and link.lower().endswith(".pdf"):
                    local_path = await self._download_file(link)
                    if local_path:
                        return await self._sum_pdf_table_column(local_path, page_number=2)

        try:
            pre = await page.query_selector("pre")
            if pre:
                t = await pre.inner_text()
                m = re.search(r'"answer"\s*:\s*"([^"]*)"', t)
                if m:
                    return m.group(1)
        except Exception:
            pass

        return None

    async def _download_file(self, url: str) -> Optional[str]:
        try:
            r = await self.client.get(url)
            r.raise_for_status()
            suffix = os.path.splitext(url)[1] or ""
            fd, path = tempfile.mkstemp(suffix=suffix)
            with os.fdopen(fd, "wb") as f:
                f.write(r.content)
            return path
        except Exception as e:
            return None

    async def _sum_pdf_table_column(self, pdf_path: str, page_number: int = 2, column_name: str = "value"):
        try:
            with pdfplumber.open(pdf_path) as pdf:
                page_index = page_number - 1
                if 0 <= page_index < len(pdf.pages):
                    page = pdf.pages[page_index]
                    table = page.extract_table()
                    if table:
                        df = pd.DataFrame(table[1:], columns=table[0])
                        matches = [c for c in df.columns if c.lower() == column_name]
                        if matches:
                            col = matches[0]
                            df[col] = pd.to_numeric(
                                df[col].str.replace(r'[^0-9.\-]', '', regex=True),
                                errors="coerce",
                            )
                            return int(df[col].sum(skipna=True))
                        numeric = df.apply(
                            lambda s: pd.to_numeric(
                                s.str.replace(r'[^0-9.\-]', '', regex=True),
                                errors="coerce",
                            )
                        )
                        return int(numeric.sum(axis=1).sum())
            return None
        except Exception:
            return None
