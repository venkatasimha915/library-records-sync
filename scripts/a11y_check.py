"""Accessibility check: loads the search page in a real browser, runs a sync, and runs axe-core
against WCAG 2.1 A/AA rules in both light and dark mode. Exits 1 if anything is found.

Usage:  python scripts/a11y_check.py http://localhost:8000 node_modules/axe-core/axe.min.js
"""

import sys

from playwright.sync_api import sync_playwright

RULES = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "best-practice"]


def main(url: str, axe_path: str) -> int:
    axe_js = open(axe_path, encoding="utf-8").read()
    problems = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for scheme in ("light", "dark"):
            page = browser.new_page(color_scheme=scheme)
            page.goto(url)
            page.click("#run-sync")
            page.wait_for_selector("#runs tr")
            page.wait_for_function("document.querySelectorAll('#results li').length > 0")
            page.add_script_tag(content=axe_js)
            result = page.evaluate("rules => axe.run(document, {runOnly: rules})", RULES)
            for v in result["violations"]:
                problems += 1
                print(f"[{scheme}] {v['impact']}: {v['id']} - {v['help']} ({len(v['nodes'])} element(s))")
            print(f"[{scheme}] {len(result['passes'])} rules passed, {len(result['violations'])} violations")
        browser.close()
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
