#!/usr/bin/env python3
"""Translate the template library to English, in place, using the Spark's Qwen.

Chinese-language titles/prompts came with the vendored awesome-gpt-image-2
collection. This walks cases.json, translates anything carrying CJK text, and
writes back after every case so an interrupted run resumes for free.
Originals are preserved as orig_title / orig_prompt.

    python3 tools/translate_templates.py            # against the Spark
    QWEN=http://127.0.0.1:8003 python3 tools/...    # override endpoint
"""
import json
import os
import re
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES = os.path.join(ROOT, "static", "template-library", "cases.json")
QWEN = os.environ.get("QWEN", "http://127.0.0.1:8003") + "/v1/chat/completions"
MODEL = "qwen3.8-27b-q4km"

CJK = re.compile(r"[　-〿぀-ヿ一-鿿＀-￯]")


def qwen(system, user, max_tokens=1600, retries=3):
    body = json.dumps({"model": MODEL, "max_tokens": max_tokens, "temperature": 0.2,
                       "messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": user}]}).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(QWEN, data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as r:
                out = json.load(r)["choices"][0]["message"]["content"] or ""
            out = re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()
            if out:
                return out
        except Exception as e:
            sys.stderr.write(f"  retry {attempt + 1}: {e}\n")
            time.sleep(3 * (attempt + 1))
    return ""


SYS_TITLE = ("You translate Chinese titles of AI image-generation templates into "
             "short natural English titles (3-8 words). Reply with ONLY the title, "
             "no quotes, no commentary.")
SYS_PROMPT = ("You translate Chinese AI image-generation prompts into natural, "
              "precise English. Keep every concrete visual instruction, camera "
              "term, and constraint; keep any placeholders or formatting. Reply "
              "with ONLY the translated prompt.")


def main():
    data = json.load(open(CASES, encoding="utf-8"))
    cases = data["cases"]
    todo = [c for c in cases if CJK.search(c.get("title") or "")
            or CJK.search(c.get("prompt") or "")]
    print(f"{len(todo)} of {len(cases)} cases still carry CJK text")
    done = 0
    for c in cases:
        changed = False
        title = c.get("title") or ""
        if CJK.search(title):
            t = qwen(SYS_TITLE, title, max_tokens=60)
            if t and not CJK.search(t):
                c.setdefault("orig_title", title)
                c["title"] = t[:160]
                if c.get("imageAlt") in ("", title):
                    c["imageAlt"] = c["title"]
                changed = True
        prompt = c.get("prompt") or ""
        if CJK.search(prompt):
            p = qwen(SYS_PROMPT, prompt, max_tokens=2400)
            if p and len(p) > 40 and not CJK.search(p):
                c.setdefault("orig_prompt", prompt)
                c["prompt"] = p[:8192]
                c["promptPreview"] = p[:200]
                changed = True
        if changed:
            done += 1
            tmp = CASES + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, CASES)
            print(f"[{done}/{len(todo)}] #{c['id']} {c['title'][:60]}", flush=True)
    print("done")


if __name__ == "__main__":
    main()
