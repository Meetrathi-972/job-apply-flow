"""
Internship auto-apply bot
-------------------------
1. Reads the SimplifyJobs Summer2027-Internships README
   (Software Engineering + Data Science/AI/ML sections)
2. Clicks each Apply button (opens in a new tab)
3. Runs your Jobright flow: custom resume -> apply now -> account/sign-in -> autofill -> continue loop -> submit
4. Goes back to the list tab, clicks "Yes, I applied", moves on to the next job

Usage (Windows, from this folder):
    pip install playwright
    python apply_bot.py setup            # ONE TIME: opens Chrome so you log into Google + Jobright
    python apply_bot.py list             # just print the jobs it found
    python apply_bot.py run --limit 2 --dry-run   # test: goes through everything but does NOT press Submit
    python apply_bot.py run --limit 20   # real run

Password is never stored in this file: set APPLY_PASSWORD env var or type it when asked.
"""
import argparse, getpass, html, json, os, re, socket, subprocess, sys, time, urllib.request
from pathlib import Path
from urllib.parse import quote, urlsplit

# ----------------------------- CONFIG ---------------------------------------
RAW_README = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/README.md"
LIST_URL = ("https://github.com/SimplifyJobs/Summer2027-Internships/blob/dev/README.md"
            "#-data-science-ai--machine-learning-internship-roles")
SECTIONS = ["Software Engineering Internship Roles",
            "Data Science, AI & Machine Learning Internship Roles"]

CANDIDATE_CONFIG_FILE = Path("candidate.json")
try:
    CANDIDATE = json.loads(CANDIDATE_CONFIG_FILE.read_text(encoding="utf-8"))
except FileNotFoundError:
    CANDIDATE = {}
except (json.JSONDecodeError, OSError) as exc:
    raise SystemExit(f"Could not read {CANDIDATE_CONFIG_FILE}: {exc}")

EMAIL = str(CANDIDATE.get("email", "")).strip()
FIRST_NAME = str(CANDIDATE.get("first_name", "")).strip()
LAST_NAME = str(CANDIDATE.get("last_name", "")).strip()
PHONE = str(CANDIDATE.get("phone", "")).strip()
RESUME_PREFIX = str(CANDIDATE.get("resume_prefix", "")).strip()
# Dedicated Chrome profile folder for the bot (Chrome blocks automation on your everyday profile).
BOT_PROFILE_DIR = str(Path.home() / "chrome-job-bot-profile")
DEBUG_PORT = 9222

RESUME_RULES = [
    (r"computer vision|image processing|visual perception", "COMPUTER_VISION_Engineer"),
    (r"data engineer|data platform|data infrastructure|data pipeline|\betl\b|database", "Data_Engineering"),
    (r"machine learning|deep learning|robot learning|reinforcement learning|\bml\b|\bai\b|artificial intelligence|\bllm\b|generative", "AI_ML_Engineer"),
    (r"data scien|analytics|data analy|statistic|business intelligence|\bbi\b", "DataScience"),
    (r"software|\bswe\b|backend|full.?stack|front.?end|developer|application technology|automation", "SDE"),
]

MAX_FORM_STEPS = 25        # max "continue" pages per application
OTP_SENDERS = {
    "icfcjb.fa.ocs.oraclecloud.com": "noreply@honeywellaerospace.com",
    "ejko.fa.us2.oraclecloud.com": "ejko.fa.sender.2@workflow.mail.us2.cloud.oracle.com",
    "egup.fa.us2.oraclecloud.com": "egup.fa.sender.1@workflow.email.us-phoenix-1.ocs.oraclecloud.com",
    "hdow.fa.us6.oraclecloud.com": "hdow.fa.sender@workflow.email.us-ashburn-1.ocs.oraclecloud.com",
}
STATE_FILE = Path("applied.json")
DEBUG_DIR = Path("debug")
# ----------------------------------------------------------------------------


# ============================ JOB LIST PARSING ================================
def fetch_jobs():
    md = urllib.request.urlopen(RAW_README, timeout=60).read().decode("utf-8")
    parts = re.split(r"\n## ", md)
    jobs = []
    for sec in SECTIONS:
        body = next((p for p in parts if sec in p.split("\n", 1)[0]), None)
        if body is None:
            print(f"[warn] section not found: {sec}")
            continue
        last_company = ""
        for row in re.findall(r"<tr>(.*?)</tr>", body, re.S):
            tds = re.findall(r"<td>(.*?)</td>", row, re.S)
            if len(tds) < 5:
                continue
            strip = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
            company = re.sub(r"[^\w\s&.,'()-]", "", strip(tds[0])).strip()
            if "↳" in tds[0] or not company:
                company = last_company
            last_company = company
            if "🔒" in row:                       # closed posting
                continue
            links = [html.unescape(h) for h in re.findall(r'href="([^"]+)"', tds[3])
                     if "simplify.jobs" not in h]
            if not links:
                continue
            jobs.append(dict(section=sec, company=company, title=strip(tds[1]),
                             location=strip(tds[2]), age=strip(tds[4]), url=links[0]))
    return jobs


def load_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ============================ CHROME / PLAYWRIGHT =============================
def chrome_path():
    for p in [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              str(Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe"),
              "/usr/bin/google-chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]:
        if os.path.exists(p):
            return p
    sys.exit("Chrome not found - edit chrome_path() in the script.")


def port_open():
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", DEBUG_PORT)) == 0


def launch_chrome(debug=True):
    args = [chrome_path(), f"--user-data-dir={BOT_PROFILE_DIR}", "--no-first-run"]
    if debug:
        args.append(f"--remote-debugging-port={DEBUG_PORT}")
    subprocess.Popen(args)


# ============================ UI HELPERS ======================================
def snap(page, name):
    DEBUG_DIR.mkdir(exist_ok=True)
    stamp = f"{int(time.time())}_{re.sub(r'[^a-z0-9]+', '_', name.lower())}"
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{stamp}.png"))
        (DEBUG_DIR / f"{stamp}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


def clear_jobright_intercept(page):
    """Remove Jobright's known click shield from an employer page before an action.

    The extension injects this fixed overlay when switching tabs. Playwright can
    see the real form control, but a normal click is intercepted by the shield.
    Only remove the extension-owned overlay; never force clicks through arbitrary
    employer dialogs or consent screens.
    """
    try:
        return page.evaluate("""() => {
          const nodes = [...document.querySelectorAll('.bring-to-foreground-overlay[data-jobright-helper-page-fixed="true"]')];
          nodes.forEach(node => node.remove());
          return nodes.length;
        }""")
    except Exception:
        return 0


def find(page, pattern, timeout=8, roles=("button", "link"), match_text=True):
    """Find first visible element whose accessible name / text matches regex, in the page AND all iframes
    (extension panels are often iframes / shadow DOM; Playwright pierces open shadow DOM)."""
    rx = re.compile(pattern, re.I)
    end = time.time() + timeout
    while True:
        for fr in page.frames:
            try:
                cands = [fr.get_by_role(r, name=rx) for r in roles]
                if match_text:
                    cands.append(fr.get_by_text(rx))
                for loc in cands:
                    for i in range(min(loc.count(), 5)):
                        el = loc.nth(i)
                        if el.is_visible():
                            return el
            except Exception:
                pass
        if time.time() >= end:
            return None
        time.sleep(0.5)


def click(page, pattern, timeout=8, label=None):
    el = find(page, pattern, timeout)
    if not el:
        return False
    try:
        el.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass
    clear_jobright_intercept(page)
    try:
        el.click(timeout=5000)
    except Exception:
        # Retry only after clearing Jobright's known injected shield. A generic
        # force click can silently bypass an employer modal or confirmation.
        clear_jobright_intercept(page)
        el.click(timeout=5000)
    print(f"    clicked: {label or pattern}")
    return True


def check_all_checkboxes(page):
    n = 0
    for fr in page.frames:
        try:
            boxes = fr.get_by_role("checkbox")
            for i in range(boxes.count()):
                b = boxes.nth(i)
                if b.is_visible() and not b.is_checked():
                    b.check(force=True, timeout=2000)
                    n += 1
        except Exception:
            pass
    print(f"    checkboxes ticked: {n}")


def has_text(page, pattern, timeout=1):
    return find(page, pattern, timeout, roles=()) is not None


# ============================ FLOW STEPS ======================================
def select_base_resume(tab, job):
    """Pick and verify a named resume in Jobright's resume editor."""
    # The editor often opens as a background tab with a zero-sized viewport.
    # Bring it forward before interacting with the resume selector.
    tab.bring_to_front()
    tab.wait_for_timeout(250)
    title = job["title"]
    track = next((name for pattern, name in RESUME_RULES if re.search(pattern, title, re.I)), None)
    if track is None:
        track = "DataScience" if job["section"].startswith("Data Science") else "SDE"
    if not RESUME_PREFIX:
        raise RuntimeError("Set resume_prefix in candidate.json to match your Jobright resume names")
    filename = f"{RESUME_PREFIX}{track}_SkillBank"
    selector = tab.locator(".ant-select-selector:visible").filter(has_text="Your resume")
    selector.wait_for(state="visible", timeout=15000)
    if filename not in selector.inner_text():
        option = tab.locator(".ant-select-item-option:visible").filter(has_text=filename)
        if not option.is_visible():
            selector.click(timeout=5000)
        option.click(timeout=5000)
    selector.filter(has_text=filename).wait_for(state="visible", timeout=10000)
    print(f"    Selected base resume: {filename}")


JOBRIGHT_EDITOR_RE = r"add a new job for this page|job details are ready to review"


def jobright_editor_open(tab):
    if has_text(tab, JOBRIGHT_EDITOR_RE, timeout=0):
        return True
    # The extension sometimes puts this editor in an isolated shadow frame;
    # its Save button is visible to Playwright while the frame text is not.
    save_button = find(tab, r"^\s*save\s*$", timeout=0,
                       roles=("button",), match_text=False)
    if not save_button:
        return False
    try:
        panel_text = save_button.evaluate(
            "e => (e.parentElement?.parentElement?.innerText || '') + ' ' + "
            "(e.getRootNode()?.host?.innerText || '')")
        return bool(re.search(JOBRIGHT_EDITOR_RE, panel_text, re.I))
    except Exception:
        return False


def save_jobright_editor(tab, timeout=12):
    """Save Jobright's parsed listing editor when its review panel is open."""
    save_button = find(tab, r"^\s*save\s*$", timeout=timeout,
                       roles=("button",), match_text=False)
    if not save_button or not jobright_editor_open(tab):
        return False
    try:
        save_button.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    clear_jobright_intercept(tab)
    save_button.click(timeout=10000)
    print("    clicked: Save Jobright listing", flush=True)
    tab.wait_for_timeout(600)
    return True


def jobright_generate_button(tab):
    """Make the add/save/retry path explicit before giving up on Jobright."""
    failed = r"failed to analyze job"
    for attempt in range(2):
        if has_text(tab, failed, timeout=0):
            if attempt:
                break
            if not click(tab, r"^\s*back\s*$", timeout=5,
                         label="Back to Jobright job details"):
                break
            tab.wait_for_timeout(700)

        # Jobright can show an "Add This Job" modal over a Generate Custom
        # Resume button. Dismiss that prompt first, then save the parsed job.
        add_prompt = find(tab, r"^\s*add this job\s*$", timeout=0,
                          roles=("button",), match_text=False)
        if add_prompt:
            click(tab, r"^\s*add this job\s*$", timeout=1,
                  label="Add This Job")
            tab.wait_for_timeout(700)

        editor = jobright_editor_open(tab)
        if editor:
            if not save_jobright_editor(tab):
                snap(tab, "jobright_save_missing")
                return None
            generate = find(tab, r"generate custom resume", timeout=45,
                            roles=("button", "link"), match_text=True)
            if generate:
                return generate

        generate = find(tab, r"generate custom resume", timeout=2,
                        roles=("button", "link"), match_text=True)
        if generate:
            return generate

        # The "Add This Job" prompt is a separate state from the parsed-job
        # editor. Save the editor, then wait for Generate Custom Resume to appear.
        added = click(tab, r"^\s*add this job\s*$", timeout=3,
                      label="Add This Job")
        if not added:
            added = click(tab, r"add this job in one click", timeout=2,
                          label="Add This Job")
        if added:
            if jobright_editor_open(tab):
                if not save_jobright_editor(tab, timeout=15):
                    snap(tab, "jobright_save_missing")
                    return None
            generate = find(tab, r"generate custom resume", timeout=45,
                            roles=("button", "link"), match_text=True)
            if generate:
                return generate

        if attempt == 0 and has_text(tab, failed, timeout=0):
            continue
        break
    return None


def jobright_resume_flow(tab, job):
    """Generate a resume and return the tab running the resume flow."""
    existing_pages = set(tab.context.pages)
    click(tab, r"^\s*(decline all|reject all|reject optional cookies)\s*$", timeout=2)
    generate = jobright_generate_button(tab)
    if not generate:
        # If analysis failed, one Back-and-retry pass is handled by
        # jobright_generate_button. Never continue toward Submit without a
        # completed Jobright resume.
        snap(tab, "no_generate_custom_resume")
        return None
    generate.click(timeout=12000)
    print("    clicked: Generate Custom Resume", flush=True)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        resume_tab = next((page for page in tab.context.pages if page not in existing_pages
                           and page.url.startswith("https://jobright.ai/jobs/info/")), None)
        if resume_tab:
            tab = resume_tab
            break
        tab.wait_for_timeout(250)
    if not tab.locator(".ant-select-selector:visible").filter(has_text="Your resume").count():
        action = find(tab, r"^fix my resume now$|^your resume\b", timeout=15)
        if action and re.fullmatch(r"fix my resume now", action.inner_text().strip(), re.I):
            action.click(timeout=5000)

    try:
        select_base_resume(tab, job)
    except Exception as error:
        print(f"    Base resume selection failed: {str(error).splitlines()[0]}")
        snap(tab, "base_resume_selection_failed")
        return None
    click(tab, r"improve my resume for this job", timeout=10)

    time.sleep(2)
    check_all_checkboxes(tab)
    if not click(tab, r"generate my new resume", timeout=10):
        snap(tab, "jobright_resume_generation_missing")
        return None
    return tab


def reset_password_via_gmail(ctx, new_password):
    """Best-effort: open Gmail in the logged-in profile, open latest reset email, set the password."""
    print("    -> resetting password via Gmail")
    mail = ctx.new_page()
    try:
        mail.goto("https://mail.google.com/mail/u/0/#search/newer_than%3A1h+(reset+OR+password)")
        time.sleep(6)
        row = mail.locator("tr.zA").first
        row.click(timeout=15000)
        time.sleep(3)
        with ctx.expect_page(timeout=15000) as pi:
            click(mail, r"reset (your )?password|reset|set (a )?new password|create (a )?new password", timeout=10)
        rp = pi.value
        rp.wait_for_load_state("domcontentloaded")
        pw = rp.locator("input[type=password]")
        for i in range(pw.count()):
            pw.nth(i).fill(new_password)
        click(rp, r"reset|submit|save|continue|update", timeout=8)
        time.sleep(4)
        rp.close()
        return True
    except Exception as e:
        print(f"    gmail reset failed: {e}")
        snap(mail, "gmail_reset_failed")
        return False
    finally:
        mail.close()


def account_step(ctx, page, password):
    """create account -> sign in (-> reset password if creds rejected)."""
    # Only act on actual password-form buttons. Broad text matching was
    # mistaking a repeated "Register" heading/link on multi-step portals for
    # an account action and clicking it on every application step.
    password_fields = page.locator('input[type=password]:visible')
    if not password_fields.count():
        return
    create = find(page, r"^\s*(?:create (?:an )?account|sign up|register)\s*$",
                  timeout=0, roles=("button",), match_text=False)
    sign_in = find(page, r"^\s*(?:sign in|log in)\s*$",
                   timeout=0, roles=("button",), match_text=False)
    if create:
        for sel in ("input[type=email]", "input[name*=mail i]", "input[autocomplete=username]"):
            f = page.locator(sel).first
            if f.count() and f.is_visible():
                f.fill(EMAIL)
                break
        for field in password_fields.all():
            if field.is_visible():
                field.fill(password)
        create.click(timeout=5000)
        print("    clicked: Create Account", flush=True)
        time.sleep(3)
        if not page.locator('input[type=password]:visible').count():
            return

    sign_in = find(page, r"^\s*(?:sign in|log in)\s*$",
                   timeout=0, roles=("button",), match_text=False)
    if sign_in and page.locator('input[type=password]:visible').count():
        for sel in ("input[type=email]", "input[name*=mail i]", "input[autocomplete=username]"):
            f = page.locator(sel).first
            if f.count() and f.is_visible():
                f.fill(EMAIL)
                break
        page.locator('input[type=password]:visible').first.fill(password)
        sign_in.click(timeout=5000)
        print("    clicked: Sign In", flush=True)
        time.sleep(3)
        if has_text(page, r"incorrect|invalid|wrong|does ?n.t match|not recognized|try again", timeout=3):
            if click(page, r"reset|forgot", timeout=3):
                time.sleep(2)
                reset_password_via_gmail(ctx, password)
                page.bring_to_front()
                page.reload()
                time.sleep(3)


def complete_email_verification(ctx, page):
    """Complete a fresh employer sign-in challenge using a known sender."""
    location = urlsplit(page.url)
    if location.hostname == 'careers.withwaymo.com':
        field = page.get_by_role('textbox', name='Please enter your validation code:', exact=True)
        if field.count() and field.is_visible():
            mail = ctx.new_page()
            try:
                query = quote('from:waymo.mail.clinchtalent.com subject:"Candidate Verification" newer_than:1h')
                mail.goto('https://mail.google.com/mail/u/0/#search/' + query,
                          wait_until='domcontentloaded', timeout=20000)
                mail.wait_for_timeout(4000)
                if EMAIL.lower() not in mail.title().lower():
                    return False
                rows = mail.locator('tr.zA').filter(has_text='Candidate Verification')
                if rows.count() != 1:
                    return False
                rows.first.click(timeout=8000)
                mail.wait_for_timeout(1000)
                code = re.search(r'Waymo careers website\.\s*([A-Z0-9]{5,8})\b', mail.locator('body').inner_text())
                if not code:
                    return False
                page.bring_to_front()
                field.fill(code[1])
                page.get_by_role('button', name='Verify', exact=True).click(timeout=8000)
                field.wait_for(state='hidden', timeout=15000)
                return True
            finally:
                mail.close()
    sender = OTP_SENDERS.get(location.hostname)
    oracle = bool(sender and "/apply/email" in location.path)
    qualcomm = location.hostname == "careers.qualcomm.com" and location.path == "/careers/apply"
    if not oracle and not qualcomm:
        return None

    if oracle:
        subject = "Confirm your identity"
        code_pattern = r"one-time pass code\s*:\s*(\d{6})\b"
        email_input = page.locator('input[name="primary-email"][type="email"]:visible')
        fields = page.locator('input[id^="pin-code-"]:visible')
        request_button, verify_button = r"^next$", r"^verify$"
    else:
        if not has_text(page, r"^check your email$|^sign in$", timeout=0):
            return None
        sender = "confirm@eightfold.ai"
        subject = "Verify your email"
        code_pattern = r"complete the verification process:\s*(\d{6})\b"
        email_input = page.locator('input[type="email"][name="email"]:visible')
        fields = page.locator('input[type="text"][autocomplete="off"]:visible')
        request_button, verify_button = r"^continue$", r"^submit$"

    requested_at = int(time.time())
    if email_input.count() == 1:
        if not email_input.input_value():
            email_input.fill(EMAIL)
        if email_input.input_value().strip().lower() != EMAIL.lower():
            return False
        if not click(page, request_button, timeout=5, label="Request employer verification"):
            return False
    else:
        requested_at -= 180

    try:
        fields.first.wait_for(state="visible", timeout=10000)
    except Exception:
        return True if oracle and "/apply/email" not in urlsplit(page.url).path else False
    if fields.count() != 6:
        return False

    query = quote(f'from:{sender} to:{EMAIL} subject:"{subject}" after:{requested_at}')
    mail = ctx.new_page()
    try:
        mail.goto(f"https://mail.google.com/mail/u/0/#search/{query}", wait_until="domcontentloaded", timeout=15000)
        deadline = time.monotonic() + 60
        code = None
        while time.monotonic() < deadline:
            if EMAIL.lower() in mail.title().lower():
                messages = mail.locator("tr.zA").filter(has_text=re.compile(re.escape(subject), re.I))
                matches = re.findall(code_pattern, "\n".join(messages.all_text_contents()), re.I)
                if matches:
                    if len(set(matches)) != 1:
                        return False
                    code = matches[0]
                    break
            mail.wait_for_timeout(3000)
            mail.reload(wait_until="domcontentloaded", timeout=15000)
        if not code:
            return False
        page.bring_to_front()
        if oracle:
            for index, digit in enumerate(code, 1):
                page.locator(f"#pin-code-{index}").fill(digit)
        else:
            fields.first.click(timeout=5000)
            page.keyboard.type(code, delay=90)
        if fields.count() and not click(page, verify_button, timeout=5, label="Verify employer email code"):
            return False
        fields.first.wait_for(state="hidden", timeout=15000)
        print("    Employer email verification completed")
        return True
    except Exception:
        return False
    finally:
        mail.close()


def ukg_google_sign_in(ctx, page):
    """Use the existing Google session on the UKG candidate login screen."""
    location = urlsplit(page.url)
    if location.hostname not in {"signin-us.ultipro.com", "signin-ca.ultipro.com", "signin-ca.ultipro.ca"} or location.path != "/u/login":
        return None
    existing_pages = set(ctx.pages)
    control = find(page, r"^continue with google$", timeout=4, match_text=False)
    if control is None:
        return None
    control.click(timeout=8000)
    print("    Started UKG Google sign-in")
    selected_account = False
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        candidates = [page] + [tab for tab in ctx.pages if tab not in existing_pages]
        for tab in candidates:
            if tab.is_closed():
                continue
            destination = urlsplit(tab.url)
            host = destination.hostname or ""
            if host.startswith("recruiting") and host.endswith((".ultipro.com", ".ultipro.ca")):
                if "/noaccess" in destination.path.casefold() or has_text(tab, r"^permission denied$", timeout=0):
                    print("    UKG denied access after Google sign-in")
                    return False
                print("    Google sign-in returned to the UKG employer portal")
                return tab
            if host != "accounts.google.com" or selected_account:
                continue
            account = tab.locator("[data-identifier]").filter(has_text=re.compile(re.escape(EMAIL), re.I))
            match = next((entry for entry in account.all() if entry.is_visible() and
                          entry.get_attribute("data-identifier").casefold() == EMAIL.casefold()), None)
            if match is not None:
                match.click(timeout=5000)
                selected_account = True
                print("    Selected the existing Google account")
        live_tab = next((tab for tab in candidates if not tab.is_closed()), None)
        if live_tab is None:
            break
        live_tab.wait_for_timeout(500)
    print("    Google sign-in needs attention in the open browser tab")
    return False


def form_errors(page):
    """Return visible validation messages without recording candidate values."""
    messages = []
    for frame in page.frames:
        try:
            errors = frame.locator('[role=alert]:visible, [aria-invalid=true]:visible')
            for el in errors.all()[:20]:
                message = el.get_attribute('aria-label') or el.inner_text() or el.get_attribute('name')
                if not message:
                    message = el.evaluate("e => Array.from(e.labels || []).map(l => l.textContent.trim()).join(' ') || (e.getAttribute('aria-labelledby') || '').split(/\\s+/).map(id => document.getElementById(id)?.textContent || '').join(' ')")
                if message and message.strip():
                    messages.append(message.strip()[:250])
        except Exception:
            continue
    return list(dict.fromkeys(messages))


def missing_required_fields(page):
    """List visible, unanswered required controls so blank autofill cannot pass."""
    missing = []
    radio_checked = {}
    for frame in page.frames:
        try:
            radio_checked.update(frame.locator('input[type=radio]:visible').evaluate_all(
                "els => Object.fromEntries([...new Set(els.map(e => e.name).filter(Boolean))].map(n => [n, els.some(e => e.name === n && e.checked)]))"))
        except Exception:
            pass
        try:
            fields = frame.locator(
                'input:visible[required],select:visible[required],textarea:visible[required],'
                'input:visible[aria-required="true"],select:visible[aria-required="true"],'
                'textarea:visible[aria-required="true"]')
            for field in fields.all()[:60]:
                try:
                    kind = (field.get_attribute('type') or '').lower()
                    name = field.get_attribute('name') or ''
                    if kind == 'file':
                        continue  # Resume attachment is checked separately at Submit.
                    if kind == 'radio' and name and radio_checked.get(name):
                        continue
                    value = field.evaluate(
                        "e => (e.type === 'checkbox' || e.type === 'radio') ? e.checked : e.value")
                    if value is not True and not str(value or '').strip():
                        label = field.get_attribute('aria-label') or field.get_attribute('placeholder') or name
                        if not label:
                            label = field.evaluate(
                                "e => Array.from(e.labels || []).map(l => l.textContent.trim()).join(' ') || "
                                "(e.getAttribute('aria-labelledby') || '').split(/\\s+/).map(id => "
                                "document.getElementById(id)?.textContent || '').join(' ')")
                        missing.append((label or 'Required field').strip()[:180])
                except Exception:
                    continue
        except Exception:
            continue
    return list(dict.fromkeys(missing))


def required_consent_missing(page):
    """Do not accept legal/privacy statements for the applicant."""
    patterns = (
        r"terms and conditions|terms of use",
        r"privacy notice|privacy policy|privacy terms|recruitment privacy|data processing consent",
        r"background check|fingerprint",
    )
    for frame in page.frames:
        for pattern in patterns:
            try:
                boxes = frame.get_by_role("checkbox", name=re.compile(pattern, re.I))
                for box in boxes.all():
                    try:
                        required = (box.get_attribute("required") is not None or
                                    box.get_attribute("aria-required") == "true")
                        if required and not box.is_checked():
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
    return False


def application_outcome(page):
    # Read both accessible content and rendered text. Some portals (notably
    # Ashby) expose confirmation in a dialog's accessible label only.
    snapshots = []
    for frame in page.frames:
        if re.search(r'hcaptcha|recaptcha', frame.url):
            continue
        try:
            snapshots.append(frame.locator('body').aria_snapshot(timeout=5000))
            snapshots.append(frame.locator('body').inner_text(timeout=3000))
        except Exception:
            if frame == page.main_frame:
                raise
            # Extension/analytics frames can detach during navigation.
    content = '\n'.join(snapshots)
    if re.search(r'already received your application|application is on file', content, re.I):
        return 'already_applied'
    if re.search(
            r'close application submitted dialog|application(?: has been)? submitted(?: successfully)?\b|'
            r'application .*submitted successfully|thank you for (?:your application|applying|submitting)|'
            r'we (?:have )?received your application|you have successfully applied', content, re.I):
        return 'applied'
    if re.search(r'We are sorry\s*,?\s*but something went wrong', content, re.I):
        return 'portal_error'
    for frame in page.frames:
        if re.search(r'hcaptcha|recaptcha', frame.url):
            try:
                if frame.frame_element().is_visible() and re.search(
                        r'please click|select all|verify you are|please try again',
                        frame.locator('body').inner_text(), re.I):
                    return 'captcha_required'
            except Exception:
                pass
    return None


def complete_ukg_profile(page):
    if '/AuthCode/Register' not in page.url:
        return False
    for label, value in [('First name', FIRST_NAME), ('Last name', LAST_NAME),
                         ('Phone number', PHONE)]:
        if not value:
            continue
        field = page.get_by_role('textbox', name=label, exact=True)
        if field.count():
            field.fill(value)
    consent = page.get_by_role('checkbox')
    if consent.count() > 1:
        return False
    # UKG's custom element exposes checked="true" but an empty aria-checked.
    # Playwright.check() otherwise clicks successfully and then reports failure.
    if consent.count() == 0:
        pass
    elif consent.get_attribute('data-tag-name') == 'checkbox':
        if consent.get_attribute('checked') != 'true':
            consent.click(timeout=8000)
        if consent.get_attribute('checked') != 'true':
            return False
    else:
        consent.check(timeout=8000)
    page.get_by_role('button', name='Create account', exact=True).click(timeout=8000)
    page.wait_for_timeout(3000)
    return '/AuthCode/Register' not in page.url


def apply_confirmed_answers(page):
    # Listings in this workflow come from SimplifyJobs, not employee referrals.
    source = page.get_by_role('combobox', name='How did you hear about this job?', exact=True)
    if source.count() == 1 and source.is_visible():
        source.click(timeout=5000)
        job_board = page.get_by_role('option', name='Job Board', exact=True)
        if job_board.count():
            job_board.click(timeout=5000)
            details = page.get_by_role('textbox', name=re.compile('^Please specify details from your answer above'))
            if details.count() == 1:
                details.fill('SimplifyJobs Summer2027-Internships on GitHub')
        else:
            source.press('Escape')
    facts_path = Path('candidate-confirmed.json')
    facts = json.loads(facts_path.read_text()) if facts_path.exists() else {}
    if facts.get('willing_to_relocate_boston') is True:
        boston = page.get_by_role('combobox', name=re.compile('^This role is based out of our Boston, MA office'))
        if boston.count() == 1:
            boston.click(timeout=5000)
            page.get_by_role('option', name='Yes', exact=True).click(timeout=5000)
    # Do not infer sensitive traits when the optional survey is marked required.
    demographics = page.get_by_role('combobox', name='Race/ethnicity (select all that apply)', exact=True)
    if demographics.count() == 1 and demographics.get_attribute('aria-invalid') == 'true':
        demographics.click(timeout=5000)
        decline = page.get_by_role('option', name="I don't wish to answer", exact=True)
        if decline.count():
            decline.click(timeout=5000)
        demographics.press('Escape')
    if facts.get('currently_holds_opt') is True:
        groups = page.locator('spl-radio-group').filter(
            has_text='Do you currently hold Optional Practical Training (OPT) work authorization?')
        if groups.count() == 1:
            groups.get_by_role('radio', name='Yes', exact=True).click(timeout=5000)
    if facts.get('spouse_or_child_government_employment_last_5_years') is False:
        groups = page.locator('spl-radio-group').filter(has_text=re.compile(
            r'D\. Within the past 5 years, has a member of your Immediate Family'))
        if groups.count() == 1:
            groups.get_by_role('radio', name='No', exact=True).click(timeout=5000)
    if facts.get('listed_export_regions_citizen_or_permanent_resident') is False:
        question = page.get_by_role('combobox', name=re.compile(
            r'^Are you a citizen or lawful permanent resident of Cuba, Syria, Iran, North Korea'))
        if question.count() == 1:
            question.click(timeout=5000)
            page.get_by_role('option', name=re.compile(r'^no$', re.I)).click(timeout=5000)
    school = page.get_by_role('combobox', name=re.compile(r'^What school do you currently attend'))
    if school.count() == 1 and facts.get('school'):
        school.click(timeout=5000)
        option = page.get_by_role('option', name=facts['school'], exact=True)
        if option.count():
            option.click(timeout=5000)
        else:
            page.get_by_role('option', name='Other', exact=True).click(timeout=5000)
            other = page.get_by_role('textbox', name=re.compile('please write your school name'))
            if other.count():
                other.fill(facts['school'])
    for frame in page.frames:
        degree = frame.get_by_role('combobox', name='What is your highest level of education completed?', exact=True)
        if degree.count() == 1 and facts.get('highest_completed_degree'):
            degree.click(timeout=5000)
            frame.get_by_role('option', name=facts['highest_completed_degree'], exact=True).click(timeout=5000)
        interviews = frame.get_by_role('combobox', name=re.compile(r'^Are you currently interviewing'))
        if interviews.count() == 1 and facts.get('active_interviews_or_offers_affecting_timeline') is False:
            interviews.click(timeout=5000)
            frame.get_by_role('option', name='No', exact=True).click(timeout=5000)
        gpa = frame.get_by_role('textbox', name=re.compile(r'current G\.?P\.?A\.?', re.I))
        if gpa.count() == 1 and facts.get('gpa'):
            gpa.fill(str(facts['gpa']))


def wait_for_autofill(page, timeout=150):
    """Keep this tab active until the extension is idle for several polls."""
    deadline = time.monotonic() + timeout
    idle_since = None
    while time.monotonic() < deadline:
        busy = find(page, r'^\s*autofilling[.\s…]*$', timeout=0,
                    roles=('button',), match_text=True)
        if busy:
            idle_since = None
        elif idle_since is None:
            idle_since = time.monotonic()
        elif time.monotonic() - idle_since >= 5:
            return True
        page.wait_for_timeout(500)
    return False


def form_signature(page):
    """Detect a Next click that leaves the same form and errors in place."""
    shapes = []
    for frame in page.frames:
        try:
            shapes.append(frame.locator('input:visible, select:visible, textarea:visible').evaluate_all(
                "els => els.map(e => [e.id,e.name,e.type,e.getAttribute('aria-invalid'), e.getAttribute('aria-label'), (e.labels || []).length ? [...e.labels].map(l => l.innerText.trim()).join(' ') : ''])"))
            shapes.append(frame.locator('h1:visible,h2:visible,[aria-current=step]:visible,[role=alert]:visible').evaluate_all(
                "els => els.map(e => (e.innerText || e.getAttribute('aria-label') || '').trim().slice(0,180))"))
        except Exception:
            pass
    return (page.url, json.dumps(shapes), tuple(form_errors(page)))


def inconsistent_start_date(page):
    title_years = re.findall(r'20\d{2}', page.title())
    if not title_years:
        return False
    job_year = int(title_years[0])
    for frame in page.frames:
        fields = frame.get_by_role('textbox', name=re.compile(r'target start date|when can you start', re.I))
        # Lever's custom textareas have no accessible question label.
        if 'jobs.lever.co' in page.url:
            fields = frame.locator('li').filter(has_text=re.compile(r'^What is your target start date')).get_by_role('textbox')
        for field in fields.all():
            years = re.findall(r'20\d{2}', field.input_value())
            if years and int(years[-1]) > job_year:
                print('    Start date falls after the internship year; user availability required')
                return True
    return False


def application_loop(ctx, page, password, dry_run):
    """autofill -> continue loop -> submit. Returns 'applied' | 'dry_run' | 'stuck'."""
    page.bring_to_front()
    for step in range(MAX_FORM_STEPS):
        print(f"    {page.title()} | Application form step {step + 1}: checking sign-in and Autofill", flush=True)
        outcome = application_outcome(page)
        if outcome:
            return outcome
        if has_text(page, r"are you still with us|session will (?:end|expire)", timeout=0):
            if click(page, r"^\s*continue working\s*$", timeout=5,
                     label="Continue Working"):
                page.wait_for_timeout(750)
                continue
            return "session_keepalive_required"
        if '/authcode/noaccess' in page.url.lower():
            return 'portal_access_denied'
        if 'Planned Outage' in page.title():
            return 'portal_outage'
        if '/AuthCode/Register' in page.url:
            if complete_ukg_profile(page):
                print('    Completed UKG account profile; checking application resume association')
                return 'custom_resume_recheck_required'
            return 'account_profile_required'
        signed_in = ukg_google_sign_in(ctx, page)
        if signed_in is False:
            return "google_sign_in_required"
        if signed_in is not None:
            page = signed_in
            page.bring_to_front()
        verified = complete_email_verification(ctx, page)
        if verified is False:
            return "email_verification_required"
        account_step(ctx, page, password)
        if has_text(page, r"^please review before continuing$", timeout=1):
            print("    employer privacy and interview policy review required")
            return "policy_review_required"

        already_autofilling = bool(find(page, r'^\s*autofilling[.\s…]*$', timeout=0,
                                       roles=('button',), match_text=True))
        if not wait_for_autofill(page):
            return 'autofill_timeout'
        for frame in page.frames:
            expanded = frame.locator('.job-profile-completion-section--open .application-dashboard-title:visible')
            if expanded.count():
                expanded.first.click(timeout=5000)
                print('    Collapsed Jobright progress panel')
        page.bring_to_front()
        clear_jobright_intercept(page)
        autofill = find(page, r'^\s*(?:account creation & )?autofill\s*$', timeout=2,
                        roles=('button',), match_text=False)
        if autofill and autofill.is_enabled() and not already_autofilling:
            autofill.click(timeout=8000)
            print('    clicked: Autofill', flush=True)
            if not wait_for_autofill(page):
                snap(page, 'autofill_timeout')
                return 'autofill_timeout'
            print('    Autofill indicator is clear')
        elif already_autofilling:
            print('    Existing Autofill completed; checking employer navigation')
        else:
            print('    Autofill unavailable; checking employer navigation')

        apply_confirmed_answers(page)
        missing = missing_required_fields(page)
        if missing and autofill:
            # A visible Autofill button can finish without populating any of
            # the required fields (seen on Lever). Retry once, then leave the
            # tab open with the missing labels instead of advancing blank data.
            print("    Required fields remain blank after Autofill; retrying once:", missing)
            page.bring_to_front()
            clear_jobright_intercept(page)
            autofill_retry = find(page, r'^\s*(?:account creation & )?autofill\s*$',
                                  timeout=2, roles=('button',), match_text=False)
            if autofill_retry and autofill_retry.is_enabled():
                autofill_retry.click(timeout=8000)
                print("    clicked: Autofill retry", flush=True)
                if not wait_for_autofill(page):
                    return "autofill_timeout"
                apply_confirmed_answers(page)
                missing = missing_required_fields(page)
        if missing:
            print("    Required answers still missing; leaving this application open:", missing)
            snap(page, "required_answers_needed")
            return "required_answers_needed"

        if required_consent_missing(page):
            print("    Required legal/privacy consent is unchecked; leaving it for the applicant")
            snap(page, "consent_required")
            return "consent_required"

        forward = find(page, r"^\s*(?:save (?:and|&) )?(?:continue(?: to the next page)?|next)\s*$",
                       timeout=7, roles=("button", "link"), match_text=False)
        if forward:
            if not forward.is_enabled():
                print('    Continue is disabled. Validation:', form_errors(page))
                return 'continue_disabled'
            before = form_signature(page)
            page.bring_to_front()
            clear_jobright_intercept(page)
            forward.click(timeout=8000)
            print("    clicked: Continue/Next")
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and form_signature(page) == before:
                page.wait_for_timeout(500)
            if form_signature(page) == before:
                print('    Continue did not advance. Validation:', form_errors(page))
                snap(page, 'continue_no_progress')
                return 'continue_no_progress'
            continue                                   # new page -> loop again

        submit = find(page, r"^\s*(submit|submit (?:your )?application)\s*$", timeout=0,
                      roles=("button",), match_text=False)
        if submit:
            for frame in page.frames:
                # Escape the slash: Playwright serializes name regexes as JS
                # selectors, where an unescaped `/` prematurely ends the regex.
                attachments = frame.get_by_role(
                    'group', name=re.compile(r'^(?:Resume|Resume\/CV)\*?$'))
                for attachment in attachments.all():
                    base_resume_pattern = (re.escape(RESUME_PREFIX) + r".*SkillBank"
                                           if RESUME_PREFIX else r"\S+_Resume_.*SkillBank")
                    if re.search(base_resume_pattern, attachment.inner_text()):
                        print('    Employer still has a base resume attached; custom resume upload must be corrected')
                        snap(page, 'custom_resume_attachment_mismatch')
                        return 'custom_resume_attachment_mismatch'
            if inconsistent_start_date(page):
                return 'availability_required'
            if not submit.is_enabled():
                snap(page, "submit_disabled")
                return "submit_disabled"
            if dry_run:
                print("    [dry-run] would click Submit here")
                return "dry_run"
            page.bring_to_front()
            clear_jobright_intercept(page)
            submit.click(timeout=8000)
            print("    clicked: Submit")
            print("    Waiting for employer confirmation")
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if urlsplit(page.url).hostname == 'careers.withwaymo.com':
                    complete_email_verification(ctx, page)
                outcome = application_outcome(page)
                if outcome:
                    return outcome
                page.wait_for_timeout(500)
            if has_text(page, r"thank you for (?:your application|applying|submitting)|application (?:has been |was )?(?:successfully )?(?:submitted|received)|we (?:have )?received your application|you have successfully applied|application (?:complete|completed)", timeout=2):
                return "applied"
            errors = form_errors(page)
            if errors:
                print('    Employer validation prevented confirmation:', errors)
                return 'validation_required'
            return "applied?"                          # clicked, but no confirmation text seen
        break
    snap(page, "stuck_in_form_loop")
    return "stuck"


def process_job(ctx, list_page, job, password, dry_run):
    print("    Opening employer listing")
    # click the Apply button on the GitHub list (Ctrl+click => opens in new tab, like the Simplify extension does)
    link = list_page.locator(f'a[href="{job["url"]}"]').first
    if not link.count():
        link = list_page.locator(f'a[href*="{job["url"].split("?")[0]}"]').first
    if link.count():
        link.scroll_into_view_if_needed(timeout=10000)
        with ctx.expect_page(timeout=15000) as pi:
            link.click(modifiers=["Control"], timeout=10000)
        job_tab = pi.value
    else:
        job_tab = ctx.new_page()
        job_tab.goto(job["url"], wait_until="domcontentloaded", timeout=20000)
    job_tab.wait_for_load_state("domcontentloaded")

    print("    Opening Jobright and preparing a tailored resume")
    resume_tab = jobright_resume_flow(job_tab, job)
    apply_tab = job_tab
    apply_tab_preopened = False
    if resume_tab is None:
        # Some ATS pages only expose Jobright after opening their application
        # form. Open Apply, then retry the custom-resume flow on that page. Never
        # continue to Autofill or Submit unless a custom resume is generated.
        apply_control = find(job_tab, r"^\s*(?:apply|apply now|apply for this position)\s*$",
                             timeout=4, roles=("button", "link"), match_text=False)
        if not apply_control:
            return "no_jobright_panel", [job_tab]
        print("    Jobright was unavailable on the listing; opening employer Apply to retry")
        try:
            existing_pages = set(ctx.pages)
            href = apply_control.get_attribute("href")
            if apply_control.get_attribute("target") == "_self" and href:
                job_tab.goto(href, wait_until="domcontentloaded", timeout=20000)
                apply_tab = job_tab
            else:
                apply_control.click(timeout=10000)
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    new_tab = next((page for page in ctx.pages
                                    if page not in existing_pages and not page.is_closed()), None)
                    if new_tab:
                        apply_tab = new_tab
                        break
                    job_tab.wait_for_timeout(250)
                if apply_tab is job_tab:
                    apply_tab.wait_for_load_state("domcontentloaded", timeout=10000)
            apply_tab.wait_for_timeout(800)
            resume_tab = jobright_resume_flow(apply_tab, job)
            apply_tab_preopened = True
        except Exception as error:
            print(f"    Employer Apply fallback failed: {str(error).splitlines()[0]}")
            snap(apply_tab, "employer_apply_fallback_failed")
            return "custom_resume_unavailable", list(dict.fromkeys([apply_tab, job_tab]))
        if resume_tab is None:
            snap(apply_tab, "custom_resume_unavailable_after_apply")
            return "custom_resume_unavailable", list(dict.fromkeys([apply_tab, job_tab]))

    print("    Opening the employer application form")
    try:
        if click(resume_tab, r"^continue to autofill$", timeout=90):
            on_form = find(apply_tab, r"^submit application$", timeout=3,
                           roles=("button",), match_text=False)
            if not apply_tab_preopened and not on_form:
                apply_control = find(job_tab, r"^\s*(?:apply(?: now| for this (?:job|position))?(?:\s*:\s*.+)?|i['’]m interested)\s*$", timeout=10,
                                     match_text=False)
                if not apply_control:
                    raise RuntimeError("employer application control not found")
                if apply_control.get_attribute("target") == "_self" and apply_control.get_attribute("href"):
                    job_tab.goto(apply_control.evaluate("link => link.href"), wait_until="domcontentloaded",
                                 timeout=20000)
                else:
                    existing_pages = set(ctx.pages)
                    apply_control.click(timeout=8000)
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        new_tab = next((page for page in ctx.pages if page not in existing_pages), None)
                        if new_tab:
                            apply_tab = new_tab
                            break
                        job_tab.wait_for_timeout(250)
        else:
            snap(resume_tab, "custom_resume_not_ready")
            return "custom_resume_not_ready", [job_tab, resume_tab]
        apply_tab.wait_for_load_state("domcontentloaded")
    except Exception as e:
        print(f"    apply-now problem: {e}")
        snap(job_tab, "apply_now_problem")
        return "no_apply_now", [job_tab]

    status = application_loop(ctx, apply_tab, password, dry_run)
    return status, list(dict.fromkeys([apply_tab, resume_tab, job_tab]))


# ============================ MAIN ============================================
def main():
    # Job titles from the README may contain emoji. Windows PowerShell can
    # otherwise give Python a cp1252 stdout that raises during normal logging.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, OSError):
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["setup", "list", "run"])
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--recent", action="store_true", help="only unattempted listings marked 0d or 1d")
    ap.add_argument("--url", help="run one exact listing URL, including a previously failed attempt")
    ap.add_argument("--queue", help="retry listings from a saved JSON queue; skip confirmed or uncertain submissions")
    ap.add_argument("--dry-run", action="store_true", help="do everything except press Submit")
    args = ap.parse_args()

    if args.mode == "setup":
        launch_chrome(debug=False)
        print("Chrome opened with the bot profile. Now: sign in to Google, install the Jobright extension,\n"
              "log into Jobright, open the Jobright extension once. Then close Chrome and run: python apply_bot.py run")
        return

    jobs = fetch_jobs()
    state = load_state()
    todo = [j for j in jobs if j["url"] not in state]
    if args.recent:
        seen_roles = {(r["company"].casefold(), r["title"].casefold()) for r in state.values()}
        recent_jobs = []
        for job in sorted(todo, key=lambda item: item["age"] != "0d"):
            role = (job["company"].casefold(), job["title"].casefold())
            if job["age"] in ("0d", "1d") and role not in seen_roles:
                recent_jobs.append(job)
                seen_roles.add(role)
        todo = recent_jobs
    if args.url:
        if args.mode != "run":
            ap.error("--url requires run mode")
        todo = [j for j in jobs if j["url"] == args.url]
        if not todo:
            ap.error("the URL is not in the current open listings")
        if state.get(args.url, {}).get("status") in ("applied", "applied?"):
            ap.error("this listing already has a recorded submission attempt")
    if args.queue:
        if args.url or args.recent:
            ap.error('--queue cannot be combined with --url or --recent')
        live = {j['url']: j for j in jobs}
        todo = []
        for job in json.loads(Path(args.queue).read_text(encoding='utf-8')):
            if job['url'] in live and state.get(job['url'], {}).get('status') not in ('applied', 'already_applied', 'applied?'):
                todo.append(live[job['url']])
    print(f"{len(jobs)} open jobs found, {len(todo)} not yet processed")
    if args.mode == "list":
        for j in todo[:50]:
            print(f"- [{j['section'][:12]}] {j['company']} | {j['title']} | {j['url'][:70]}")
        return

    missing_config = [key for key, value in {
        "email": EMAIL, "first_name": FIRST_NAME, "last_name": LAST_NAME,
        "phone": PHONE, "resume_prefix": RESUME_PREFIX,
    }.items() if not value]
    if missing_config:
        ap.error("candidate.json is missing: " + ", ".join(missing_config) +
                 ". Copy candidate.example.json to candidate.json and fill in your details.")

    password = os.environ.get("APPLY_PASSWORD") or getpass.getpass("Password for account creation/sign-in: ")

    from playwright.sync_api import sync_playwright
    if not port_open():
        launch_chrome(debug=True)
        for _ in range(30):
            if port_open():
                break
            time.sleep(1)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
        ctx = browser.contexts[0]
        list_page = next((page for page in ctx.pages if page.url.split("#", 1)[0] == LIST_URL.split("#", 1)[0]), None)
        def ensure_list_page(page):
            nonlocal list_page
            try:
                if page is not None and not page.is_closed():
                    page.bring_to_front()
                    return page
            except Exception:
                pass
            print("    README tab was closed; reopening the job list")
            list_page = ctx.new_page()
            list_page.goto(LIST_URL, wait_until="domcontentloaded", timeout=30000)
            return list_page

        list_page = ensure_list_page(list_page)

        for n, job in enumerate(todo[:args.limit], 1):
            print(f"\n[{n}/{min(args.limit, len(todo))}] {job['company']} - {job['title']}")
            tabs = []
            try:
                list_page = ensure_list_page(list_page)
                status, tabs = process_job(ctx, list_page, job, password, args.dry_run)
            except Exception as e:
                status = f"error: {e}"
                snap(list_page, "exception")
            print(f"    => {status}")
            open_tab_reasons = {
                "captcha_required": "CAPTCHA requires your input; application left open without retrying the challenge.",
                "portal_error": "Employer returned a server error; completed fields remain in the open tab.",
                "availability_required": "Autofill used a start date after the internship year; actual availability is required.",
                "custom_resume_recheck_required": "UKG account completed; employer application needs its own custom resume association before continuing.",
                "account_profile_required": "UKG needs profile details and privacy consent before creating the account.",
                "portal_access_denied": "The employer portal denied access after authentication.",
                "portal_outage": "The employer portal reports a planned outage.",
                "continue_disabled": "The employer Continue button is disabled; see validation details above.",
                "continue_no_progress": "Continue was clicked but the form did not advance; see validation details above.",
                "validation_required": "The employer displayed validation errors after Submit; submission is not confirmed.",
                "no_jobright_panel": "Jobright could not complete the resume flow. See the most recent Jobright debug capture.",
                "custom_resume_not_ready": "Jobright did not show the completed resume handoff. The employer application was not advanced.",
                "custom_resume_unavailable": "Apply was opened, but Jobright still could not generate the required custom resume.",
                "no_apply_now": "The bot could not open the employer application form.",
                "policy_review_required": "The employer requires a policy or privacy choice.",
                "consent_required": "A required legal or privacy checkbox needs the applicant's acknowledgment.",
                "email_verification_required": "The employer verification code could not be matched or accepted.",
                "google_sign_in_required": "Google sign-in did not return to the employer portal.",
                "autofill_unavailable": "Jobright's Autofill button was unavailable.",
                "autofill_timeout": "Jobright still showed Autofilling when the wait expired.",
                "required_answers_needed": "Required fields remained blank after one Autofill retry.",
                "session_keepalive_required": "The employer session needs manual reactivation.",
                "submit_disabled": "The employer's Submit button was disabled.",
                "stuck": "The form offered no usable Continue or Submit control.",
                "applied?": "Submit was clicked, but the employer did not display a confirmation.",
            }
            if status not in ("applied", "already_applied", "dry_run"):
                print(f"    Left any opened application tabs available. {open_tab_reasons.get(status, 'The run encountered the error shown above.')}")
            if not args.dry_run:
                state = load_state()
                state[job["url"]] = dict(status=status, company=job["company"], title=job["title"])
                save_state(state)
            if status == "autofill_timeout":
                print("Batch paused while Jobright is still autofilling.")
                break

            list_page = ensure_list_page(list_page)
            if not args.dry_run and status == "applied":
                click(list_page, r"yes,? i applied|^yes$", timeout=4, label="Yes, I applied")
            if status in ("applied", "dry_run"):   # leave unconfirmed tabs open for review
                for t in tabs:
                    try:
                        t.close()
                    except Exception:
                        pass
        print("\nDone. Results in applied.json, screenshots of failures in ./debug/")


if __name__ == "__main__":
    main()
