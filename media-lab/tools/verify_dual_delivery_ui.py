"""Offline Chromium regressions over exact production download/render functions.
Run under an owned resource lease. Requires Playwright + installed Chrome.
No server, real user records, network requests, or model calls are used.
"""

import argparse
import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--chrome", required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    source = (Path(__file__).parents[1] / "static/index.html").read_text()
    css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", source, re.DOTALL))
    begin = source.index("function deliveryDownloads(")
    end = source.index("/* everything rowDetail()", begin)
    production = source[begin:end]
    settings = source[
        source.index("function settingsLine(") : source.index("/* Both variants are explicit:")
    ]
    globals_js = """
    const QD={history:[]},CATALOG={enhance_ready:false},STYLE_LABELS={},CHAR_LABELS={};
    const isAdmin=false;
    function esc(s){return String(s||'').replace(/</g,'&lt;')}
    function copyIdButton(){return ''}
    const saves=[];function saveHref(e,a){e.preventDefault();saves.push(a.getAttribute('href'));return false}
    const pending=[];
    window.fetch=()=>new Promise(resolve=>pending.push(resolve));
    function resolveGallery(items){pending.shift()({ok:true,json:async()=>items})}
    """
    html = (
        '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        "<style>" + css + '</style><main style="padding:16px;max-width:600px;margin:auto">'
        '<h2>Finished video</h2><section id="detail"></section>'
        '<div class="row"><a id="save" class="btn2" download>Save</a></div></main>'
        "<script>" + globals_js + settings + production + "</script>"
    )
    job = {
        "id": "synthetic",
        "kind": "video",
        "status": "done",
        "prompt": "A bright mountain trail — synthetic QA fixture",
        "url": "/media/synthetic.mp4",
        "variants": {"hdr": "/media/synthetic.mp4", "sdr": "/media/synthetic.sdr.mp4"},
        "request": {"duration": 15, "model": "h3"},
    }
    checks = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=args.chrome, headless=True)
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.route(
                "**/*",
                lambda route: (
                    route.fulfill(status=200, content_type="text/html", body=html)
                    if route.request.url == "https://media-lab.test/"
                    else route.abort()
                ),
            )
            page.goto("https://media-lab.test/")
            page.evaluate(
                "j=>{QD.history=[j];document.querySelector('#detail').innerHTML=rowDetail(j.id)}",
                job,
            )
            assert page.get_by_role("link", name="Save HDR", exact=True).count() == 1
            assert page.get_by_role("link", name="Save vibrant SDR", exact=True).count() == 1
            page.get_by_role("link", name="Save HDR", exact=True).click()
            page.get_by_role("link", name="Save vibrant SDR", exact=True).click()
            assert page.evaluate("saves") == ["/media/synthetic.mp4", "/media/synthetic.sdr.mp4"]
            checks.append("exact production job-detail links select distinct files")
            page.evaluate(
                "j=>{const a=document.querySelector('#save');a.href=j.url;a.onclick=e=>saveHref(e,a);configureDeliveryDownloads(a,j.url);resolveGallery([j])}",
                job,
            )
            page.wait_for_timeout(30)
            assert page.locator("[data-sdr-download]").is_visible()
            for width in (320, 390, 1280):
                page.set_viewport_size({"width": width, "height": 900})
                assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
                for name in ("Save HDR", "Save vibrant SDR"):
                    for link in page.get_by_role("link", name=name, exact=True).all():
                        assert link.is_visible()
                        box = link.bounding_box()
                        assert box and box["x"] >= 0 and box["x"] + box["width"] <= width
                page.screenshot(path=str(args.output / f"downloads-{width}.png"))
            checks.append("320/390/1280px links visible, in bounds, without horizontal overflow")
            page.set_viewport_size({"width": 640, "height": 900})
            page.evaluate("document.body.style.zoom='2'")
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            page.screenshot(path=str(args.output / "downloads-text200.png"))
            checks.append("200% browser CSS zoom remains in bounds")
            page.evaluate("document.body.style.zoom=''")
            contrast = {}
            for theme in ("coagent", "paper"):
                page.evaluate("t=>document.documentElement.dataset.theme=t", theme)
                page.wait_for_timeout(
                    250
                )  # Allow the existing 180ms theme/color transition to settle.
                contrast[theme] = page.evaluate(r"""() => {
                  const rgba=c=>(c.match(/[0-9.]+/g)||[]).map(Number);
                  const blend=(top,bottom)=>top.slice(0,3).map((c,i)=>c*(top[3]??1)+bottom[i]*(1-(top[3]??1)));
                  const lum=c=>c.map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4}).reduce((s,v,i)=>s+v*[.2126,.7152,.0722][i],0);
                  return [...document.querySelectorAll('a[download]')].map(a=>{
                    const chain=[];for(let e=a;e;e=e.parentElement)chain.unshift(e);
                    let bg=[255,255,255];for(const e of chain)bg=blend(rgba(getComputedStyle(e).backgroundColor),bg);
                    const fg=blend(rgba(getComputedStyle(a).color),bg), x=lum(fg),y=lum(bg);
                    return {label:a.textContent,ratio:(Math.max(x,y)+.05)/(Math.min(x,y)+.05)};
                  });
                }""")
                assert all(value["ratio"] >= 4.5 for value in contrast[theme]), contrast
                page.screenshot(path=str(args.output / f"downloads-{theme}.png"))
            checks.append("dark/light save labels meet 4.5:1 computed composited contrast")
            page.emulate_media(forced_colors="active")
            page.screenshot(path=str(args.output / "downloads-forced-colors.png"))
            page.emulate_media(forced_colors="none")
            page.locator("#detail").get_by_role("link", name="Save HDR", exact=True).focus()
            page.keyboard.press("Tab")
            assert page.evaluate("document.activeElement.textContent") == "Save vibrant SDR"
            page.keyboard.press("Enter")
            assert page.evaluate("saves[saves.length-1]") == "/media/synthetic.sdr.mp4"
            checks.append("keyboard Tab and Enter select SDR")
            assert page.evaluate("deliveryDownloads({url:'/media/legacy.mp4'})").count("<a ") == 1
            assert page.evaluate("deliveryDownloads({url:'javascript:alert(1)'})") == ""
            assert (
                page.evaluate(
                    "deliveryDownloads({url:'/media/a.mp4',variants:{hdr:'/media/a.mp4',sdr:'/media/a.mp4'}})"
                ).count("<a ")
                == 1
            )
            checks.append("legacy, missing variant, same-file and unsafe-URL fallbacks")
            page.evaluate("""() => {
              const a=document.querySelector('#save');
              configureDeliveryDownloads(a,'/media/first.mp4');
              configureDeliveryDownloads(a,'/media/second.mp4');
              resolveGallery([{url:'/media/first.mp4',variants:{hdr:'/media/first.mp4',sdr:'/media/first.sdr.mp4'}}]);
            }""")
            page.wait_for_timeout(20)
            assert page.locator("[data-sdr-download]").is_hidden()
            page.evaluate(
                "resolveGallery([{url:'/media/second.mp4',variants:{hdr:'/media/second.mp4',sdr:'/media/second.sdr.mp4'}}])"
            )
            page.wait_for_timeout(20)
            assert (
                page.locator("[data-sdr-download]").get_attribute("href") == "/media/second.sdr.mp4"
            )
            assert page.locator("[data-sdr-download]").is_visible()
            page.evaluate(
                "configureDeliveryDownloads(document.querySelector('#save'),'/media/legacy.mp4');resolveGallery([])"
            )
            page.wait_for_timeout(20)
            assert page.locator("[data-sdr-download]").is_hidden()
            assert page.locator("#save").text_content() == "Save"
            checks.append(
                "late first-clip reply cannot overwrite second selection; legacy hides stale SDR"
            )
            page.evaluate(
                "configureDeliveryDownloads(document.querySelector('#save'),'/media/error.mp4');pending.shift()({ok:false})"
            )
            page.wait_for_timeout(20)
            assert page.locator("[data-sdr-download]").is_hidden()
            checks.append("unavailable metadata retains single Save action")
        finally:
            browser.close()
    result = {
        "checks": checks,
        "checks_passed": len(checks),
        "contrast": contrast,
        "browser_closed": True,
        "scope": "Exact production component/functions and CSS with synthetic API fixtures; not live-device or HDR-display proof",
    }
    (args.output / "ui-checks.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
