"""Attended apply-assist agent (Phase 5, lite).

Opens a REAL, visible browser that YOU control, fills an application form from your
saved profile, highlights what it touched, then STOPS. It never clicks submit and
never logs in for you -- you review and submit yourself.

Deliberately NOT stealthy: no headless, no anti-detection, no captcha solving, no
proxies. You are sitting in front of it, so none of that is needed -- and that is the
whole point. Run it only on applications you intend to complete yourself.

Usage:
    python -m app.apply.filler "https://careers.example.com/job/123/apply"

Profile: copy apply_profile.example.json -> apply_profile.json and fill it in.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = ROOT / "apply_profile.json"
USER_DATA_DIR = ROOT / ".browser"  # persists your login between runs (gitignored)

# Map a profile field -> keywords we look for in a form field's label/name/placeholder.
# More specific rules first so "first name" wins over the generic "name".
FIELD_RULES = [
    ("first_name", ["first name", "given name", "fname"]),
    ("last_name", ["last name", "surname", "family name", "lname"]),
    ("full_name", ["full name", "your name", "name"]),
    ("email", ["email", "e-mail"]),
    ("phone", ["phone", "mobile", "contact number", "tel"]),
    ("linkedin_url", ["linkedin"]),
    ("github_url", ["github"]),
    ("portfolio_url", ["portfolio", "website", "personal site"]),
    ("location", ["location", "current city", "where are you based", "city"]),
]

# Runs in the page: tags each fillable field and returns a descriptor for matching.
DESCRIPTOR_JS = """
() => {
  const out = [];
  let i = 0;
  document.querySelectorAll('input, textarea, select').forEach(el => {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image', 'checkbox', 'radio'].includes(type)) return;
    el.setAttribute('data-aa-idx', i);
    let label = '';
    if (el.id) {
      const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l) label += ' ' + l.innerText;
    }
    const wrap = el.closest('label');
    if (wrap) label += ' ' + wrap.innerText;
    const desc = [label, el.getAttribute('aria-label'), el.getAttribute('placeholder'),
                  el.getAttribute('name'), el.id].filter(Boolean).join(' ').toLowerCase();
    out.push({
      idx: i, tag: el.tagName.toLowerCase(), type, desc,
      required: el.required || el.getAttribute('aria-required') === 'true'
    });
    i++;
  });
  return out;
}
"""


def load_profile() -> dict:
    if not PROFILE_PATH.exists():
        sys.exit(
            f"No profile at {PROFILE_PATH}\n"
            "Copy apply_profile.example.json -> apply_profile.json and fill it in."
        )
    data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    if data.get("full_name") and not data.get("first_name"):
        parts = data["full_name"].split()
        data["first_name"] = parts[0]
        data["last_name"] = " ".join(parts[1:]) if len(parts) > 1 else ""
    return data


def fill_form(page, profile: dict) -> dict:
    """Best-effort fill. Returns what was filled vs. skipped. Never submits."""
    fields = page.evaluate(DESCRIPTOR_JS)
    answers = {k.lower(): v for k, v in (profile.get("answers") or {}).items()}
    resume = profile.get("resume_path")
    filled, skipped = [], []

    for f in fields:
        loc = page.locator(f"[data-aa-idx='{f['idx']}']")
        desc, ftype, tag = f["desc"], f["type"], f["tag"]
        try:
            if ftype == "file":
                if resume and any(k in desc for k in ("resume", "cv", "upload", "attach")):
                    loc.set_input_files(resume)
                    filled.append((desc[:60] or "(resume)", "<resume file>"))
                else:
                    skipped.append((desc[:60] or "(file input)", f["required"]))
                continue

            value = None
            for key, keywords in FIELD_RULES:
                if any(kw in desc for kw in keywords):
                    value = profile.get(key)
                    break
            if value is None:  # try free-text answers by keyword overlap
                for akey, aval in answers.items():
                    if akey and akey in desc:
                        value = aval
                        break
            if value is None and tag == "textarea" and "cover" in desc:
                value = profile.get("cover_letter")

            if value:
                loc.fill(str(value))
                loc.evaluate("el => el.style.outline = '3px solid #22c55e'")  # green = filled
                filled.append((desc[:60], str(value)[:50]))
            else:
                if f["required"]:
                    loc.evaluate("el => el.style.outline = '3px solid #eab308'")  # yellow = needs you
                skipped.append((desc[:60] or f"({tag})", f["required"]))
        except Exception as e:  # one bad field must not abort the rest
            skipped.append((f"{desc[:50]} [error: {e}]", f.get("required", False)))

    return {"filled": filled, "skipped": skipped}


def print_report(report: dict) -> None:
    print("\n=== FILLED (green outline) ===")
    for desc, val in report["filled"]:
        print(f"  [+] {desc!r:62} -> {val}")
    if not report["filled"]:
        print("  (nothing matched -- this form's fields didn't map to your profile)")
    print("\n=== SKIPPED (fill these yourself; yellow = required) ===")
    for desc, required in report["skipped"]:
        print(f"  [ ] [{'REQUIRED' if required else 'optional'}] {desc}")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit('Usage: python -m app.apply.filler "<application_url>"')
    url = sys.argv[1]
    profile = load_profile()

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=False,                 # visible, on purpose
            args=["--start-maximized"],
            no_viewport=True,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url, wait_until="domcontentloaded")

        input(
            "\n>> Browser is open. Log in if needed and navigate to the actual "
            "application FORM.\n>> When the form is on screen, press Enter and I'll fill it... "
        )
        print_report(fill_form(page, profile))
        input(
            "\n>> I have NOT submitted anything. Review every field, fix anything wrong, "
            "then click Submit YOURSELF.\n>> Press Enter here when you're done to close the browser... "
        )
        ctx.close()


if __name__ == "__main__":
    main()
