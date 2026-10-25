"""Check the dashboard card in WebKit (Safari's engine) against a fake Home Assistant.

Renders the made-up demo home (tools/make_demo_map.py), serves it and the card
through Playwright's request routing (no web server), then clicks through the
card and checks the service calls it makes. Screenshots go to local/card_harness/.

    python tools/card_harness/check_card.py           # needs playwright (pip) and its webkit
    python tools/card_harness/check_card.py --race    # also the registry-swap race (fetches the polyfill)
"""

from __future__ import annotations

import json
import pathlib
import sys

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import make_demo_map as demo  # noqa: E402

HERE = pathlib.Path(__file__).parent
CARD = ROOT / "custom_components" / "eufy_clean_local" / "frontend" / "eufy-clean-card.js"
OUT = ROOT / "local" / "card_harness"
POLYFILL = "https://cdn.jsdelivr.net/npm/@webcomponents/scoped-custom-element-registry@0.0.10/scoped-custom-element-registry.min.js"

STATE = """() => { const r = document.querySelector('eufy-clean-card').shadowRoot;
  return { tabs: [...r.querySelectorAll('.tab')].map(t => t.textContent.trim() + (t.classList.contains('on') ? ' [shown]' : '') + (t.querySelector('.here') ? ' [robot]' : '')),
           status: r.querySelector('.status').textContent, img: (r.querySelector('#map img').getAttribute('src') || '').split('?')[0],
           polygons: r.querySelectorAll('polygon.room').length, disabledChips: r.querySelectorAll('.chip[disabled]').length,
           note: ((r.querySelector('.note') || {}).textContent || '').replace(/\\s+/g, ' ').trim() }; }"""


def demo_assets() -> tuple[bytes, list[dict]]:
    snap = demo.build_snapshot(False)
    renderer = demo.mapdata.MapRenderer.build(snap)
    names = {r["id"]: r["name"] for r in snap.rooms()}
    shapes = [{"id": rid, "name": names[rid], "points": pts} for rid, pts in sorted(renderer.shapes.items())]
    return renderer.frame(snap.pose(), snap.path()), shapes


def check(name: str, ok: bool, detail="") -> bool:
    print(f"{'ok  ' if ok else 'FAIL'} {name}{f': {detail}' if detail else ''}")
    return ok


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    png, shapes = demo_assets()
    html = (HERE / "harness.html").read_text().replace("__SHAPES__", json.dumps(shapes))
    race_html = ('<!doctype html><script>import("/card.js"); setTimeout(() => { const s = document.createElement("script");'
                 f's.src = "{POLYFILL}"; s.onload = () => {{ customElements.define("home-assistant", class extends HTMLElement {{}});'
                 'window.__booted = true; }; document.head.append(s); }, 300);</script>')

    def serve(route) -> None:
        url = route.request.url
        if url.endswith("/card.js"):
            route.fulfill(body=CARD.read_text(), content_type="text/javascript")
        elif "/api/image_proxy/" in url:
            route.fulfill(body=png, content_type="image/png")
        elif url.endswith("/race"):
            route.fulfill(body=race_html, content_type="text/html")
        else:
            route.fulfill(body=html, content_type="text/html")

    results = []
    with sync_playwright() as p:
        browser = p.webkit.launch()
        page = browser.new_page(viewport={"width": 560, "height": 1100})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("http://eufy.test/**", serve)
        page.goto("http://eufy.test/")
        page.wait_for_function("() => { const c = document.querySelector('eufy-clean-card');"
                               " const i = c && c.shadowRoot && c.shadowRoot.querySelector('#map img');"
                               " return i && i.complete && i.naturalWidth > 0; }", timeout=15000)
        page.screenshot(path=str(OUT / "card.png"), full_page=True)

        # One floor: tap two rooms on the map, clean them; change suction; toggle BoostIQ.
        page.locator("eufy-clean-card polygon[data-room='2']").click()
        page.locator("eufy-clean-card polygon[data-room='6']").click()
        page.locator("eufy-clean-card").screenshot(path=str(OUT / "card-selected.png"))
        page.locator("eufy-clean-card button[data-action='clean-rooms']").click()
        page.locator("eufy-clean-card .seg button[data-value='Max']").click()
        page.locator("eufy-clean-card button[data-action='toggle']").click()
        page.wait_for_timeout(200)
        calls = page.evaluate("window.calls")
        results.append(check("room clean of the tapped rooms", calls[0] == {
            "d": "vacuum", "s": "send_command", "data": {"command": "room_clean", "params": {"rooms": [2, 6], "clean_times": 1}},
            "target": {"entity_id": "vacuum.l60"}}, calls[0]))
        results.append(check("suction and BoostIQ", [c["s"] for c in calls[1:]] == ["set_fan_speed", "toggle"]))

        # Two floors: the robot is upstairs; downstairs shows its last map without room tapping.
        page.evaluate("""() => {
          const shapes = window.hass.states['image.l60_map'].attributes.room_shapes;
          push({
            'image.l60_map': ['2026-10-25T10:00:00+00:00', {floor: 'Upstairs',
              floors: [{id: 7, name: 'Downstairs', active: false}, {id: 9, name: 'Upstairs', active: true}]}],
            'vacuum.l60': ['docked', {floor: 'Upstairs'}],
            'image.l60_downstairs_map': ['2026-10-20T10:00:00+00:00', {access_token: 'down', map_id: 7, floor: 'Downstairs', active: false, room_shapes: shapes.slice(0, 3)}, 'floor_map'],
            'image.l60_upstairs_map': ['2026-10-25T10:00:00+00:00', {access_token: 'up', map_id: 9, floor: 'Upstairs', active: true, room_shapes: shapes}, 'floor_map'],
          });
        }""")
        page.wait_for_timeout(300)
        up = page.evaluate(STATE)
        results.append(check("floor tabs, robot upstairs", up["tabs"] == ["Downstairs", "Upstairs [shown] [robot]"]
                             and up["status"].endswith("· Upstairs") and up["polygons"] == 6, up))
        page.locator("eufy-clean-card").screenshot(path=str(OUT / "card-floors.png"))
        page.locator("eufy-clean-card .tab", has_text="Downstairs").click()
        page.wait_for_timeout(300)
        down = page.evaluate(STATE)
        results.append(check("downstairs tab shows its own map, rooms not tappable",
                             down["img"] == "/api/image_proxy/image.l60_downstairs_map" and down["polygons"] == 0
                             and down["disabledChips"] == 3 and "robot is on Upstairs" in down["note"], down))
        n = len(page.evaluate("window.calls"))
        page.locator("eufy-clean-card .chip[disabled]").first.click(force=True)
        page.locator("eufy-clean-card").screenshot(path=str(OUT / "card-other-floor.png"))
        results.append(check("no call for a room on another floor", len(page.evaluate("window.calls")) == n))
        page.locator("eufy-clean-card .tab", has_text="Upstairs").click()
        page.wait_for_timeout(300)
        back = page.evaluate(STATE)
        results.append(check("back upstairs: live map and rooms", back["img"] == "/api/image_proxy/image.l60_map"
                             and back["polygons"] == 6, back))
        results.append(check("no script errors", not errors, errors))

        if "--race" in sys.argv:
            race = browser.new_page()
            race.route("http://eufy.test/**", serve)
            race.goto("http://eufy.test/race")
            race.wait_for_function("() => window.__booted", timeout=15000)
            race.wait_for_timeout(1500)
            results.append(check("registers after Home Assistant's registry swap",
                                 race.evaluate("() => !!window.customElements.get('eufy-clean-card')")))
        browser.close()
    print(f"screenshots in {OUT}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
