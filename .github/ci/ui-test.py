"""The panel in a real browser, on every push: what the unit tests cannot see - a script error
that stops a tab from drawing (a TDZ bug in the map did exactly that for weeks), a form that
does not save, a layout that scrolls sideways on a phone. Runs against the panel the Docker job
just installed.  python3 ui-test.py http://127.0.0.1:2560 admin <password>"""
import sys
from playwright.sync_api import sync_playwright

URL, USER, PASS = sys.argv[1], sys.argv[2], sys.argv[3]
failures = []


def check(ok, what):
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)


with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1400, "height": 1000})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: m.type == "error" and "Failed to load resource" not in m.text and errors.append(m.text))

    pg.goto(URL)
    pg.fill("#u", USER); pg.fill("#p", "definitely-wrong"); pg.click("#b"); pg.wait_for_timeout(2500)
    check("Wrong" in pg.inner_text("#e") or "Zły" in pg.inner_text("#e"), "a wrong password is refused with a message")
    pg.fill("#p", PASS); pg.click("#b"); pg.wait_for_timeout(3500)
    check(pg.locator("#tabs").is_visible(), "logs in")
    ver = open("panel/VERSION").read().strip()
    check(pg.inner_text("#attrib-ver") == ver, f"the footer shows the version ({pg.inner_text('#attrib-ver')!r}, want {ver!r})")

    tabs = pg.locator("#tabs a")
    names = [tabs.nth(i).inner_text().strip() for i in range(tabs.count())]
    check(len(names) >= 10, f"all tabs are there ({len(names)})")
    for i, name in enumerate(names):
        before = len(errors)
        tabs.nth(i).click(); pg.wait_for_timeout(900)
        pane = tabs.nth(i).get_attribute("data-pane")
        check(pg.locator(f"#{pane}").is_visible() and len(errors) == before,
              f"tab '{name}' draws without a script error" + (f": {errors[before:]}" if len(errors) > before else ""))

    pg.locator("#tabs a[data-pane=t-settings]").click(); pg.wait_for_timeout(800)
    old = pg.input_value("#vh-s-name")
    pg.fill("#vh-s-name", "CI Browser Test"); pg.click("#vh-set-save"); pg.wait_for_timeout(2500)
    pg.reload(); pg.wait_for_timeout(3000); pg.locator("#tabs a[data-pane=t-settings]").click(); pg.wait_for_timeout(800)
    check(pg.input_value("#vh-s-name") == "CI Browser Test", "a setting saves and survives a reload")
    pg.fill("#vh-s-name", old); pg.click("#vh-set-save"); pg.wait_for_timeout(2000)

    pg.locator("#tabs a[data-pane=t-alerts]").click(); pg.wait_for_timeout(1200)
    check(pg.input_value("#al-topic") != "", "the Alerts tab loads its settings (an empty form once saved as empty)")

    pg.locator("[data-lang=pl]").click(); pg.wait_for_timeout(1000)
    check("Ustawienia" in pg.inner_text("#tabs"), "the Polish interface")
    pg.locator("[data-lang=en]").click(); pg.wait_for_timeout(800)

    m = b.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    m.on("pageerror", lambda e: errors.append(str(e)))
    m.goto(URL); m.fill("#u", USER); m.fill("#p", PASS); m.click("#b"); m.wait_for_timeout(3500)
    mt = m.locator("#tabs a")
    for i in range(mt.count()):
        mt.nth(i).click(); m.wait_for_timeout(600)
        w = m.evaluate("document.documentElement.scrollWidth")
        check(w <= 392, f"phone: '{mt.nth(i).inner_text().strip()}' fits the screen ({w}px)")

    pg.locator("#logout").click(); pg.wait_for_timeout(1500)
    check(pg.locator("#u").is_visible(), "logs out")
    check(not errors, f"no script errors anywhere ({errors[:3]})")
    b.close()

print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
